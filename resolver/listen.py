#!/usr/bin/env python3
"""Push wakeup: hold the SSE stream and poke the resolver unit on assignment.

The timer path polls: worst-case pickup latency is the full timer interval. This
daemon closes that gap to about a second by holding an outbound connection to
`GET /api/events/stream` and starting `stingray-resolver.service` when a ticket
lands in the bot's queue.

**Why the resolver dials out.** The documented topology is Stingray on a server
and the resolver on a dev station, which is behind NAT and cannot receive an
inbound webhook. Inverting the direction needs no open port and no tunnel.

**Why it pokes systemd instead of calling `sweep()`.** systemd already
serializes starts of a unit and merges a start job into one that is already
queued, so delegating gets no-overlap and burst debouncing for free — and
`resolve_tickets.py` needs zero changes to gain push wakeup. A poke is a
*hint*: the sweep re-queries the API and is idempotent, so a redundant one
costs a wasted round trip and nothing else.

**A stream outage degrades to today's behaviour, never to a stalled resolver.**
Reconnects back off to a cap and keep retrying forever, and the timer stays
installed as the safety net — for missed events, for a listener that is down,
and for tickets that become actionable with no event at all (a due date
passing). Nothing here is load-bearing for correctness.

Run it under `stingray-resolver-listen.service`, or by hand:

    ./listen.py                 # follow the stream, poke for real
    ./listen.py --dry-run       # log what it would poke, start nothing
    ./listen.py --once          # exit after the first poke (smoke test)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from typing import Any, Iterator, Optional

import requests

import audit
from config import Config, station_name
from stingray import StingrayClient

# Event types worth waking for. Everything else on the stream (status changes,
# comments, agent runs) is a ticket already moving, which is either the
# resolver's own work echoing back or a human's — neither needs a sweep.
WAKE_EVENTS = frozenset({"ticket.assigned"})

DEFAULT_UNIT = "stingray-resolver.service"

# How long to hold the door open after the first event before poking. Bulk
# reassignment fans out one event per ticket; the sweep works the whole queue in
# one pass, so waiting a beat turns a burst of N into one start.
COALESCE_SECONDS = 2.0

# Read timeout on the stream. The server heartbeats every 15s, so silence for
# appreciably longer than that means the connection is dead — usually a proxy or
# a NAT table that dropped it without sending a FIN, which is exactly the case a
# socket read would otherwise block on forever.
READ_TIMEOUT = 45.0
CONNECT_TIMEOUT = 15.0

RECONNECT_BACKOFF_START = 2.0
RECONNECT_BACKOFF_CAP = 300.0

# How often to tell the server this identity is alive. The sweep also
# heartbeats, but only while it is sweeping — so with a 30-minute timer a
# perfectly healthy resolver went quiet for half an hour at a time and the
# roster called it stale. This process is the one that is always up, which
# makes it the honest source of liveness. The value is reported alongside the
# beat so a reader can size "too quiet" from the cadence instead of guessing.
HEARTBEAT_SECONDS = 300.0


def log(msg: str) -> None:
    audit.get_logger().info(msg)


class _Formatter(logging.Formatter):
    """Plain lines, with the API key scrubbed the same way a sweep scrubs it."""

    def format(self, record: logging.LogRecord) -> str:
        return audit.redact(super().format(record))


def setup_logging(cfg: Config) -> None:
    """Log to stdout only — systemd captures it into the journal.

    Deliberately *not* ``audit.setup_logging``: that opens a per-sweep log file
    and an audit jsonl, which suits a process that starts, does one unit of work
    and exits. This one runs for weeks and its whole output is connection
    bookkeeping, so a file per run would be a file that never rotates.
    """
    audit.register_secret(cfg.api_key)
    logger = audit.get_logger()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)


# --- SSE parsing ------------------------------------------------------------

def parse_sse(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Turn a stream of decoded lines into ``{id, event, data}`` frames.

    A blank line dispatches the frame; a line starting with ``:`` is a comment
    (the server's heartbeat) and is dropped, though merely *arriving* is the
    signal that the connection is alive. Unparseable ``data:`` is passed through
    as None rather than killing the connection — a frame this client does not
    understand is not a reason to stop following the stream.
    """
    event: Optional[str] = None
    event_id: Optional[str] = None
    data_lines: list[str] = []

    for line in lines:
        line = line.rstrip("\r")
        if line.startswith(":"):
            continue
        if line == "":
            if event is not None or data_lines:
                raw = "\n".join(data_lines)
                try:
                    data = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    data = None
                yield {"id": event_id, "event": event, "data": data}
            event, event_id, data_lines = None, None, []
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)
        # Any other field (`retry:`) is not something this client acts on.


def is_wakeup(frame: dict[str, Any], bot_user_id: int) -> bool:
    """True when ``frame`` means work landed in this bot's queue.

    The server already applied its read boundary, so everything on the stream is
    something this key may see — but that boundary is "created by me OR assigned
    to me", which is wider than "mine to work". The narrowing to ``assigned_to``
    happens here so the endpoint stays a general-purpose tail.
    """
    if frame.get("event") not in WAKE_EVENTS:
        return False
    data = frame.get("data")
    if not isinstance(data, dict):
        # An assignment event we could not parse: wake anyway. The sweep filters
        # by assignee itself, so the cost of being wrong is one no-op run, while
        # the cost of ignoring it is a ticket sitting until the timer fires.
        return True
    return data.get("assigned_to") == bot_user_id


# --- liveness ---------------------------------------------------------------

class Heartbeat:
    """Tells the server this identity is alive, on its own thread.

    Its own thread and its own client because the reader is blocked on the
    stream for minutes at a time and ``requests.Session`` is not thread-safe —
    the same reasoning the sweep's lease heartbeat uses.

    Every failure is swallowed. Liveness reporting is a convenience for a human
    reading the roster; a server that is down, a key that was rotated, or an
    older server with no such endpoint must not take the listener with it.
    """

    def __init__(self, cfg: Config, station: str, unit: str,
                 interval: float = HEARTBEAT_SECONDS,
                 stop: Optional[threading.Event] = None):
        self.cfg = cfg
        self.station = station
        self.unit = unit
        self.interval = interval
        self._stop = stop or threading.Event()
        self._client = StingrayClient(cfg.stingray_url, cfg.api_key,
                                      logger=audit.get_logger())
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._warned = False
        self.beats = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def beat(self) -> bool:
        """One check-in. Returns whether it landed."""
        try:
            # No `effective_config`: the sweep owns that half of the row, and
            # the server applies only the fields a caller actually sends. Sending
            # an empty snapshot here would blank what the sweep reported.
            self._client.heartbeat(
                label=self.cfg.env_file,
                name=self.cfg.name,
                agent=self.cfg.agent,
                model=self.cfg.agent_model or self.cfg.agent_implement_model or "",
                station=self.station,
                heartbeat_seconds=int(self.interval),
            )
        except Exception as exc:  # noqa: BLE001 - never take the listener down
            if not self._warned:
                # Once, not every interval: a server that refuses this will
                # refuse it forever, and a line every five minutes for a
                # cosmetic feature is how a log stops being read.
                log(f"listen: heartbeat failed, continuing without one: {exc!r}")
                self._warned = True
            return False
        self._warned = False
        self.beats += 1
        return True

    def _run(self) -> None:
        self.beat()
        while not self._stop.wait(self.interval):
            self.beat()


# --- the poke ---------------------------------------------------------------

class Waker:
    """Coalesces a burst of events into one `systemctl start`.

    Runs on its own thread so the coalescing window never blocks the reader —
    events that arrive mid-window have to keep being collected, or the window
    would swallow them instead of merging them.
    """

    def __init__(self, unit: str, dry_run: bool = False, once: bool = False,
                 stop: Optional[threading.Event] = None, systemctl_user: bool = False):
        self.unit = unit
        self.dry_run = dry_run
        self.once = once
        # Which systemd manager owns the sweep unit. A resolver installed under
        # `systemctl --user` is the common case on a dev station: the agent CLIs
        # need this account's credentials, and a user unit needs no polkit rule
        # for a non-root listener to start it — but the poke then has to be
        # addressed to the user manager, or it silently targets the system one.
        self.systemctl_user = systemctl_user
        # Shared with the reader, so `--once` tears the whole daemon down rather
        # than leaving the connection held open by a thread with nothing to do.
        self._stop = stop or threading.Event()
        self._pending = threading.Event()
        self.pokes = 0
        self._thread = threading.Thread(target=self._run, name="waker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def notify(self) -> None:
        self._pending.set()

    def stop(self) -> None:
        self._stop.set()
        self._pending.set()  # unblock the wait so the thread can see the stop

    def _run(self) -> None:
        while not self._stop.is_set():
            self._pending.wait()
            if self._stop.is_set():
                return
            # Hold the window open, collecting whatever else arrives.
            if self._stop.wait(COALESCE_SECONDS):
                return
            # Clear *before* poking, not after: an event that lands while the
            # sweep is starting describes work that start may be too late to
            # see, so it has to survive as a fresh pending flag.
            self._pending.clear()
            self.poke()
            if self.once:
                self._stop.set()
                return

    def poke(self) -> None:
        self.pokes += 1
        scope = "--user " if self.systemctl_user else ""
        if self.dry_run:
            log(f"listen: would start {scope}{self.unit} (dry run)")
            return
        # --no-block returns as soon as the job is queued. Without it this would
        # sit for the whole sweep, since the unit is Type=oneshot and `start`
        # waits for it; with it, systemd merges a start for an already-running
        # unit into the queued job, which is the debouncing this relies on.
        cmd = ["systemctl"] + (["--user"] if self.systemctl_user else []) \
            + ["start", "--no-block", self.unit]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            log("listen: systemctl not found — is this host running systemd?")
            return
        except subprocess.TimeoutExpired:
            log(f"listen: `systemctl {scope}start {self.unit}` timed out")
            return
        if result.returncode != 0:
            log(f"listen: `systemctl {scope}start {self.unit}` failed rc={result.returncode}: "
                f"{(result.stderr or '').strip()}")
        else:
            log(f"listen: poked {self.unit}")


# --- the stream -------------------------------------------------------------

def follow(cfg: Config, waker: Waker, stop: threading.Event,
           last_event_id: Optional[str] = None) -> Optional[str]:
    """Hold one connection until it drops. Returns the last event id seen.

    Raised exceptions are the caller's signal to back off and reconnect; the
    cursor is returned so the next attempt can resume where this one stopped
    rather than skipping the gap.
    """
    # STINGRAY_URL already carries the `/api` prefix (config.py only strips the
    # trailing slash, and every other consumer — StingrayClient, file_ticket's
    # printed link — appends bare paths to it). Adding another one here made
    # every connection 404 and back off forever, so the listener looked healthy
    # in `systemctl status` while the resolver stayed timer-only.
    url = f"{cfg.stingray_url}/events/stream"
    params = {"last_event_id": last_event_id} if last_event_id else {}
    headers = {"X-API-Key": cfg.api_key, "Accept": "text/event-stream"}

    with requests.get(url, headers=headers, params=params, stream=True,
                      timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as response:
        response.raise_for_status()
        log(f"listen: connected to {url}"
            + (f" (resuming after {last_event_id})" if last_event_id else ""))
        for frame in parse_sse(response.iter_lines(decode_unicode=True)):
            if stop.is_set():
                break
            if frame.get("id"):
                last_event_id = frame["id"]
            if is_wakeup(frame, cfg.bot_user_id):
                data = frame.get("data") or {}
                log(f"listen: {frame.get('event')} ticket={data.get('ticket_id')} "
                    f"-> waking the resolver")
                waker.notify()
    return last_event_id


def run(cfg: Config, waker: Waker, stop: threading.Event) -> None:
    """Follow the stream forever, reconnecting with backoff on any drop."""
    backoff = RECONNECT_BACKOFF_START
    last_event_id: Optional[str] = None

    while not stop.is_set():
        try:
            last_event_id = follow(cfg, waker, stop, last_event_id)
            # A clean return means the server closed the stream. That is not an
            # error, but reconnecting instantly would spin if it keeps happening.
            backoff = RECONNECT_BACKOFF_START
            reason = "stream closed by the server"
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            reason = f"HTTP {status}"
            if status in (401, 403):
                # A bad key will never fix itself by retrying sooner. Keep
                # retrying anyway (the key may be rotated back in) but at the cap.
                log("listen: the API key was rejected — check STINGRAY_API_KEY")
                backoff = RECONNECT_BACKOFF_CAP
        except requests.RequestException as exc:
            reason = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - the daemon must outlive surprises
            reason = f"unexpected {type(exc).__name__}: {exc}"

        if stop.is_set():
            break
        log(f"listen: disconnected ({reason}); reconnecting in {backoff:.0f}s. "
            "The resolver timer covers the gap.")
        if stop.wait(backoff):
            break
        backoff = min(backoff * 2, RECONNECT_BACKOFF_CAP)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--unit", default=os.environ.get("RESOLVER_UNIT", DEFAULT_UNIT),
                        help=f"systemd unit to start on an event (default {DEFAULT_UNIT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="log the pokes instead of starting anything")
    parser.add_argument("--once", action="store_true",
                        help="exit after the first poke (smoke test)")
    parser.add_argument("--station", default=None,
                        help="name of the host this resolver runs on "
                             "(default: $RESOLVER_STATION, else the hostname)")
    parser.add_argument("--no-heartbeat", action="store_true",
                        help="don't report liveness to the server")
    parser.add_argument("--systemctl-user", action="store_true",
                        default=os.environ.get("RESOLVER_SYSTEMCTL_USER", "") not in ("", "0"),
                        help="poke the per-user systemd manager (`systemctl --user`) "
                             "instead of the system one; required when the sweep is "
                             "installed as a user unit")
    args = parser.parse_args(argv)
    if args.station is None:
        args.station = station_name()

    cfg = Config.load()
    setup_logging(cfg)
    log(f"listen: following {cfg.stingray_url} as user {cfg.bot_user_id} "
        f"on station {args.station}, "
        f"unit={args.unit}{' (--user)' if args.systemctl_user else ''}"
        f"{' (dry run)' if args.dry_run else ''}")

    stop = threading.Event()
    waker = Waker(args.unit, dry_run=args.dry_run, once=args.once, stop=stop,
                  systemctl_user=args.systemctl_user)
    waker.start()

    heartbeat: Optional[Heartbeat] = None
    if not args.no_heartbeat:
        heartbeat = Heartbeat(cfg, args.station, args.unit, stop=stop)
        heartbeat.start()

    def _shutdown(signum, _frame):
        log(f"listen: caught {signal.Signals(signum).name}, shutting down")
        stop.set()
        waker.stop()
        if heartbeat is not None:
            heartbeat.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        run(cfg, waker, stop)
    except KeyboardInterrupt:
        pass
    finally:
        waker.stop()
        if heartbeat is not None:
            heartbeat.stop()
    log("listen: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
