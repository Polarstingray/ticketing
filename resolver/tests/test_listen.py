"""Tests for the SSE push-wakeup listener (``listen.py``).

Three seams, all driven without a network or a systemd: the SSE framing, the
"is this mine to wake for" filter, and the coalescing waker. The waker tests are
the interesting ones — they assert that a burst becomes one poke *and* that an
event arriving while a poke is in flight still gets its own, which is the
property that keeps a ticket from being silently dropped into the timer's lap.
"""
import threading
import time

import pytest
import requests

import listen
from listen import Waker, is_wakeup, parse_sse, run

BOT = 2


def _frames(text: str) -> list[dict]:
    return list(parse_sse(iter(text.split("\n"))))


# --- SSE parsing ------------------------------------------------------------

def test_parse_sse_reads_a_frame():
    frames = _frames('id: 12\nevent: ticket.assigned\ndata: {"ticket_id": 5}\n\n')
    assert frames == [{"id": "12", "event": "ticket.assigned", "data": {"ticket_id": 5}}]


def test_parse_sse_drops_comments():
    """The server's keepalive is a comment; it must not surface as a frame."""
    assert _frames(": keepalive\n\n: connected at 3\n\n") == []


def test_parse_sse_reads_several_frames():
    frames = _frames(
        'id: 1\nevent: a\ndata: {}\n\n'
        ': keepalive\n\n'
        'id: 2\nevent: b\ndata: {"x": 1}\n\n'
    )
    assert [f["id"] for f in frames] == ["1", "2"]
    assert frames[1]["data"] == {"x": 1}


def test_parse_sse_tolerates_unparseable_data():
    """A frame this client can't read is not a reason to drop the connection."""
    frames = _frames("id: 4\nevent: ticket.assigned\ndata: not json\n\n")
    assert frames == [{"id": "4", "event": "ticket.assigned", "data": None}]


def test_parse_sse_joins_multiline_data():
    frames = _frames('event: x\ndata: {"a":\ndata: 1}\n\n')
    assert frames[0]["data"] == {"a": 1}


def test_parse_sse_ignores_unknown_fields():
    frames = _frames("retry: 5000\nevent: ticket.assigned\ndata: {}\n\n")
    assert frames[0]["event"] == "ticket.assigned"


def test_parse_sse_drops_an_unterminated_frame():
    """A connection cut mid-frame yields nothing rather than half an event."""
    assert _frames('event: ticket.assigned\ndata: {"ticket_id": 5}') == []


# --- the wake filter --------------------------------------------------------

def test_is_wakeup_for_my_assignment():
    frame = {"event": "ticket.assigned", "data": {"ticket_id": 5, "assigned_to": BOT}}
    assert is_wakeup(frame, BOT) is True


def test_is_wakeup_ignores_someone_elses_assignment():
    """The server's boundary is created-by OR assigned-to; this narrows it."""
    frame = {"event": "ticket.assigned", "data": {"ticket_id": 5, "assigned_to": 99}}
    assert is_wakeup(frame, BOT) is False


def test_is_wakeup_ignores_unassignment():
    frame = {"event": "ticket.assigned", "data": {"ticket_id": 5, "assigned_to": None}}
    assert is_wakeup(frame, BOT) is False


@pytest.mark.parametrize("event", ["ticket.created", "ticket.status_changed",
                                   "comment.created", "agent_run.finished"])
def test_is_wakeup_ignores_other_event_types(event):
    """Those describe a ticket already moving; none of them needs a sweep."""
    assert is_wakeup({"event": event, "data": {"assigned_to": BOT}}, BOT) is False


def test_is_wakeup_errs_toward_waking_on_an_unreadable_assignment():
    """Cost of a false wake is one no-op sweep; of a missed one, a stalled ticket."""
    assert is_wakeup({"event": "ticket.assigned", "data": None}, BOT) is True


# --- the coalescing waker ---------------------------------------------------

class RecordingWaker(Waker):
    """A Waker whose poke is recorded instead of shelling out to systemctl."""

    def __init__(self, **kw):
        super().__init__("unit.service", **kw)
        self.poked = threading.Event()
        self.at: list[float] = []

    def poke(self):
        self.pokes += 1
        self.at.append(time.monotonic())
        self.poked.set()


@pytest.fixture
def fast_coalesce(monkeypatch):
    """Shrink the burst window so the timing tests stay quick."""
    monkeypatch.setattr(listen, "COALESCE_SECONDS", 0.1)
    return 0.1


def test_waker_pokes_once_for_one_event(fast_coalesce):
    waker = RecordingWaker()
    waker.start()
    try:
        waker.notify()
        assert waker.poked.wait(2), "the waker never poked"
        assert waker.pokes == 1
    finally:
        waker.stop()


def test_waker_coalesces_a_burst(fast_coalesce):
    """Bulk reassignment fans out one event per ticket; the sweep handles all."""
    waker = RecordingWaker()
    waker.start()
    try:
        for _ in range(25):
            waker.notify()
        assert waker.poked.wait(2)
        time.sleep(fast_coalesce * 4)
        assert waker.pokes == 1
    finally:
        waker.stop()


def test_waker_pokes_again_for_an_event_after_the_window(fast_coalesce):
    """A later event is separate work, not part of the burst already handled."""
    waker = RecordingWaker()
    waker.start()
    try:
        waker.notify()
        assert waker.poked.wait(2)
        waker.poked.clear()
        waker.notify()
        assert waker.poked.wait(2)
        assert waker.pokes == 2
    finally:
        waker.stop()


def test_waker_does_not_poke_without_an_event(fast_coalesce):
    waker = RecordingWaker()
    waker.start()
    try:
        time.sleep(fast_coalesce * 5)
        assert waker.pokes == 0
    finally:
        waker.stop()


def test_waker_stops_cleanly(fast_coalesce):
    waker = RecordingWaker()
    waker.start()
    waker.stop()
    waker._thread.join(timeout=2)
    assert not waker._thread.is_alive()


def test_waker_once_stops_the_shared_stop(fast_coalesce):
    """`--once` must tear the reader down too, not just the waker thread."""
    stop = threading.Event()
    waker = RecordingWaker(once=True, stop=stop)
    waker.start()
    try:
        waker.notify()
        assert waker.poked.wait(2)
        assert stop.wait(2), "--once left the reader running"
    finally:
        waker.stop()


def test_poke_shells_out_with_no_block(monkeypatch):
    """`--no-block` is what lets systemd merge starts instead of us blocking."""
    calls = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(listen.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or Result())
    Waker("stingray-resolver.service").poke()

    assert calls == [["systemctl", "start", "--no-block", "stingray-resolver.service"]]


def test_poke_addresses_the_user_manager(monkeypatch):
    """`--systemctl-user` puts `--user` before the verb.

    Order matters: `systemctl start --user` is parsed as a *unit named* --user by
    older systemd, so the flag has to lead. Without it the poke silently targets
    the system manager, which a non-root listener may not start — the sweep then
    never runs and only the timer covers the identity.
    """
    calls = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(listen.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or Result())
    Waker("stingray-resolver@claude-lite.service", systemctl_user=True).poke()

    assert calls == [["systemctl", "--user", "start", "--no-block",
                      "stingray-resolver@claude-lite.service"]]


def test_poke_dry_run_starts_nothing(monkeypatch):
    monkeypatch.setattr(listen.subprocess, "run",
                        lambda *a, **kw: pytest.fail("dry run shelled out"))
    waker = Waker("stingray-resolver.service", dry_run=True)
    waker.poke()
    assert waker.pokes == 1


def test_poke_survives_a_failing_systemctl(monkeypatch):
    """A failed start is logged, not raised — the daemon has to keep listening."""
    class Result:
        returncode = 1
        stderr = "Unit not found."

    monkeypatch.setattr(listen.subprocess, "run", lambda *a, **kw: Result())
    Waker("nope.service").poke()  # must not raise


def test_poke_survives_a_host_without_systemctl(monkeypatch):
    def _boom(*a, **kw):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(listen.subprocess, "run", _boom)
    Waker("nope.service").poke()  # must not raise


# --- reconnect --------------------------------------------------------------

class FakeCfg:
    # With the `/api` prefix, the way a real STINGRAY_URL is written.
    stingray_url = "http://stingray.test/api"
    api_key = "sk_testkeytestkey"
    bot_user_id = BOT


class FakeResponse:
    """Enough of `requests.Response` for `follow` to read a canned stream."""

    def __init__(self, body: str):
        self._body = body
        self.calls: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=False):
        return iter(self._body.split("\n"))


def _fake_get(body: str, captured: dict):
    def _get(url, headers=None, params=None, stream=None, timeout=None):
        captured.update(url=url, headers=headers, params=params,
                        stream=stream, timeout=timeout)
        return FakeResponse(body)
    return _get


def test_follow_wakes_on_my_assignment_and_returns_the_cursor(monkeypatch):
    captured: dict = {}
    body = (
        ": connected at 40\n\n"
        'id: 41\nevent: ticket.created\ndata: {"ticket_id": 9, "assigned_to": 2}\n\n'
        'id: 42\nevent: ticket.assigned\ndata: {"ticket_id": 9, "assigned_to": 2}\n\n'
    )
    monkeypatch.setattr(listen.requests, "get", _fake_get(body, captured))
    waker = RecordingWaker()

    cursor = listen.follow(FakeCfg(), waker, threading.Event())

    assert waker._pending.is_set()
    assert cursor == "42"
    assert captured["url"] == "http://stingray.test/api/events/stream"
    assert captured["headers"]["X-API-Key"] == FakeCfg.api_key
    assert captured["stream"] is True


def test_follow_sends_the_resume_cursor(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(listen.requests, "get", _fake_get("", captured))

    listen.follow(FakeCfg(), RecordingWaker(), threading.Event(), last_event_id="99")

    assert captured["params"] == {"last_event_id": "99"}


def test_follow_omits_the_cursor_on_a_fresh_connection(monkeypatch):
    """No cursor means "start at the head" — don't replay the whole outbox."""
    captured: dict = {}
    monkeypatch.setattr(listen.requests, "get", _fake_get("", captured))

    listen.follow(FakeCfg(), RecordingWaker(), threading.Event())

    assert captured["params"] == {}


def test_follow_does_not_wake_for_another_bot(monkeypatch):
    body = 'id: 8\nevent: ticket.assigned\ndata: {"ticket_id": 9, "assigned_to": 77}\n\n'
    monkeypatch.setattr(listen.requests, "get", _fake_get(body, {}))
    waker = RecordingWaker()

    listen.follow(FakeCfg(), waker, threading.Event())

    assert not waker._pending.is_set()


def test_run_reconnects_with_growing_backoff(monkeypatch):
    """A dropped stream must degrade to the timer's cadence, not to a stall."""
    waits: list[float] = []
    attempts = []
    stop = threading.Event()

    def fake_follow(cfg, waker, stop_event, last_event_id=None):
        attempts.append(last_event_id)
        raise requests.ConnectionError("refused")

    def fake_wait(seconds):
        waits.append(seconds)
        if len(waits) >= 3:
            stop.set()
        return stop.is_set()

    monkeypatch.setattr(listen, "follow", fake_follow)
    monkeypatch.setattr(stop, "wait", fake_wait)
    run(FakeCfg(), RecordingWaker(), stop)

    assert len(attempts) == 3
    assert waits == [2.0, 4.0, 8.0]


def test_run_backoff_is_capped(monkeypatch):
    stop = threading.Event()
    waits: list[float] = []

    monkeypatch.setattr(listen, "RECONNECT_BACKOFF_CAP", 10.0)
    monkeypatch.setattr(listen, "follow",
                        lambda *a, **kw: (_ for _ in ()).throw(requests.Timeout("slow")))
    monkeypatch.setattr(stop, "wait", lambda s: waits.append(s) or len(waits) >= 8)
    run(FakeCfg(), RecordingWaker(), stop)

    assert max(waits) == 10.0


def test_run_resumes_from_the_last_event_id(monkeypatch):
    """Reconnecting must cover the gap, not skip whatever landed during it."""
    seen: list[str | None] = []
    stop = threading.Event()

    def fake_follow(cfg, waker, stop_event, last_event_id=None):
        seen.append(last_event_id)
        return "77"  # the server closed the stream after event 77

    monkeypatch.setattr(listen, "follow", fake_follow)
    monkeypatch.setattr(stop, "wait", lambda s: len(seen) >= 2)
    run(FakeCfg(), RecordingWaker(), stop)

    assert seen == [None, "77"]


def test_run_stops_when_asked(monkeypatch):
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(listen, "follow",
                        lambda *a, **kw: pytest.fail("connected despite stop"))
    run(FakeCfg(), RecordingWaker(), stop)


def test_run_survives_an_unexpected_error(monkeypatch):
    """The daemon must outlive a surprise; a crash means no push wakeup at all."""
    stop = threading.Event()
    waits = []
    monkeypatch.setattr(listen, "follow",
                        lambda *a, **kw: (_ for _ in ()).throw(ValueError("surprise")))
    monkeypatch.setattr(stop, "wait", lambda s: waits.append(s) or True)
    run(FakeCfg(), RecordingWaker(), stop)
    assert waits  # it backed off and would have retried, rather than propagating
