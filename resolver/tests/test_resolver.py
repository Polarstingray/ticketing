"""Unit tests for the resolver hardening + audit logging.

No network, no subprocesses, no real Claude — every external edge is stubbed.
"""
import json
import logging

import pytest
import requests

import audit
import resolve_tickets as rt
import stingray
from conftest import BOT, FakeClient


# --- B1: resilient Stingray client --------------------------------------
class FakeResp:
    def __init__(self, status=200, json_data=None, headers=None):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client(outcomes, **kw):
    c = stingray.StingrayClient("http://x", "sk_test_unit_key_123456", backoff_base=0, **kw)
    c.session = FakeSession(outcomes)
    return c


def test_retries_5xx_then_succeeds():
    c = _client([FakeResp(503), FakeResp(502), FakeResp(200, {"ok": True})], max_retries=3)
    assert c._request("GET", "/tickets").json() == {"ok": True}
    assert c.session.calls == 3


def test_does_not_retry_404():
    c = _client([FakeResp(404)], max_retries=3)
    with pytest.raises(requests.HTTPError):
        c._request("GET", "/tickets/1")
    assert c.session.calls == 1  # no wasted retries on a deterministic 4xx


def test_retries_connection_error_then_gives_up():
    c = _client([requests.ConnectionError()] * 3, max_retries=3)
    with pytest.raises(requests.ConnectionError):
        c._request("GET", "/x")
    assert c.session.calls == 3


def test_honors_retry_after_on_429(monkeypatch):
    slept = []
    monkeypatch.setattr(stingray.time, "sleep", lambda s: slept.append(s))
    c = _client([FakeResp(429, headers={"Retry-After": "2"}), FakeResp(200, {})], max_retries=2)
    c._request("GET", "/x")
    assert slept and max(slept) >= 2


# --- B3: attempt cap -----------------------------------------------------
def test_attempt_cap_gives_up_after_max(fake_cfg):
    fails = [{"author": BOT, "body": f"{rt.FAIL_MARKER} this ticket.\n\nboom"} for _ in range(3)]
    client = FakeClient(fails)
    ticket = {"id": 7, "tags": [], "status": "open", "created_by": 9}

    rt.process(fake_cfg, client, ticket, dry_run=False)

    assert any("Giving up" in body for _, body in client.comments_added)
    last = client.updates[-1]
    assert last["status"] == "open"
    assert last["assigned_to"] == 9
    assert all(not t.startswith("claude:") for t in last["tags"])


def test_attempt_streak_resets_after_a_posted_plan(fake_cfg):
    # Two failures, then a successful plan, then one failure: streak is 1, not 3.
    comments = [
        {"author": BOT, "body": f"{rt.FAIL_MARKER} this ticket."},
        {"author": BOT, "body": f"{rt.FAIL_MARKER} this ticket."},
        {"author": BOT, "body": f"{rt.PLAN_MARKER} (Stingray resolver)\n\nplan"},
        {"author": BOT, "body": f"{rt.FAIL_MARKER} this ticket."},
    ]
    assert rt.recent_failures(comments, BOT) == 1


# --- B2: command-failure guards in publish ------------------------------
def test_push_failure_does_not_open_pr_or_fake_success(fake_cfg, monkeypatch):
    calls = []

    def fake_run(cmd, cwd=None, timeout=None):
        calls.append(cmd)
        if "push" in cmd:
            return 1, "fatal: remote rejected"
        return 0, ""

    monkeypatch.setattr(rt, "run", fake_run)
    client = FakeClient()
    ticket = {"id": 5, "tags": [], "status": "in_review", "created_by": 3}

    rt.publish(fake_cfg, client, ticket, fake_cfg.logs_dir / "repo",
               fake_cfg.logs_dir / "wt", "claude/ticket-5", "HEAD", "main",
               "summary", "1 file", origin=True, pr_ok=True)

    assert not any(c and c[0] == "gh" for c in calls), "must not run gh after a failed push"
    assert any(rt.FAIL_MARKER in body for _, body in client.comments_added)
    assert not any(rt.IMPL_MARKER in body for _, body in client.comments_added)
    # reimplementable: handed back awaiting plan approval, not left as a PR.
    assert client.updates[-1]["tags"] == [rt.TAG_AWAIT_PLAN]
    assert client.updates[-1]["status"] == "in_review"


# --- B4: reviewer feedback threaded into rework -------------------------
def test_implement_prompt_includes_reviewer_notes(tmp_path):
    notes = "Please rename foo() to bar() and add a test."
    prompt = rt.implement_prompt(
        {"id": 1, "title": "t", "description": "d"}, tmp_path, plan="THE PLAN",
        reviewer_notes=notes)
    assert notes in prompt
    assert "reviewer" in prompt.lower()
    assert "THE PLAN" in prompt


# --- A1: secret redaction ------------------------------------------------
def test_redact_removes_api_key_and_tokens():
    audit.register_secret("sk_supersecret_value_001")
    text = ("url X-API-Key: sk_supersecret_value_001 "
            "and ghp_ABCDEFGHIJKLMNOP1234 bearer abcdef0123456789")
    out = audit.redact(text)
    assert "sk_supersecret_value_001" not in out
    assert "ghp_ABCDEFGHIJKLMNOP1234" not in out
    assert "abcdef0123456789" not in out


def test_human_formatter_scrubs_secrets():
    fmt = audit._HumanFormatter("%(message)s")
    rec = logging.LogRecord("resolver", logging.INFO, __file__, 1,
                            "leaking ghp_ABCDEFGHIJKLMNOP1234", None, None)
    assert "ghp_ABCDEFGHIJKLMNOP1234" not in fmt.format(rec)


# --- A4: stream-json parsing --------------------------------------------
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_stream_json_tool_use_becomes_audit_events():
    logger = logging.getLogger("resolver.streamtest")
    logger.setLevel(logging.DEBUG)
    cap = _Capture()
    logger.handlers = [cap]
    logger.propagate = False

    result = {}
    rt._claude_event(logger, None, json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "thinking"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "timeout 180 pytest -q"}},
        ]},
    }), result)
    rt._claude_event(logger, None, json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": "all done",
    }), result)

    tools = [r.audit for r in cap.records if getattr(r, "audit", {}).get("kind") == "claude_tool"]
    assert len(tools) == 1
    assert tools[0]["tool"] == "Bash"
    assert "pytest -q" in tools[0]["input_summary"]
    assert result.get("result") == "all done"


def test_summarize_tool_use_shapes():
    assert rt.summarize_tool_use("Read", {"file_path": "/a/b.py"}) == "/a/b.py"
    assert rt.summarize_tool_use("Write", {"file_path": "/x.py"}) == "/x.py"
    assert rt.summarize_tool_use("Grep", {"pattern": "foo", "path": "src"}) == "foo in src"


def test_run_claude_handles_missing_binary(tmp_path, fake_cfg):
    fake_cfg.claude_bin = "definitely-not-a-real-binary-xyz"
    fake_cfg.claude_model = ""
    fake_cfg.implement_tools = "Read"
    fake_cfg.claude_timeout = 5
    fake_cfg.claude_implement_timeout = 5
    log_path = tmp_path / "out.log"
    ok, msg = rt.run_claude(fake_cfg, "hi", tmp_path, "plan", log_path)
    assert ok is False
    assert "definitely-not-a-real-binary-xyz" in msg


# --- agent-run tracking (#56): POST per-phase usage to the backend ----------
class RunRecordingClient:
    """Captures create_agent_run calls; optionally raises to test resilience."""

    def __init__(self, raise_on_post=False):
        self.runs: list[tuple[int, dict]] = []
        self.raise_on_post = raise_on_post

    def create_agent_run(self, ticket_id, **fields):
        self.runs.append((ticket_id, fields))
        if self.raise_on_post:
            raise requests.ConnectionError("backend down")
        return {"id": len(self.runs)}


def _tracking_cfg():
    from types import SimpleNamespace
    return SimpleNamespace(agent="claude", claude_model="claude-opus-4-8")


def test_run_agent_tracked_posts_captured_usage(monkeypatch):
    """The usage emitted deep inside the runner (via _emit_token_usage) is
    bridged out and POSTed as a succeeded AgentRun for the phase."""
    def fake_run_agent(cfg, prompt, cwd, mode, log_path):
        rt._emit_token_usage(logging.getLogger("resolver"), "claude", mode, {
            "input_tokens": 1000, "output_tokens": 200,
            "cache_read_tokens": 50, "cache_write_tokens": 10,
            "cost_usd": 0.0123, "model": "claude-opus-4-8",
        })
        return True, "done"

    monkeypatch.setattr(rt, "run_agent", fake_run_agent)
    client = RunRecordingClient()
    ok, text = rt.run_agent_tracked(_tracking_cfg(), client, {"id": 7}, "p",
                                    None, "plan", None)
    assert (ok, text) == (True, "done")
    assert len(client.runs) == 1
    ticket_id, fields = client.runs[0]
    assert ticket_id == 7
    assert fields["phase"] == "plan"
    assert fields["agent"] == "claude"
    assert fields["status"] == "succeeded"
    assert fields["input_tokens"] == 1000
    assert fields["cost_usd"] == 0.0123
    assert fields["model"] == "claude-opus-4-8"
    assert fields["started_at"] and fields["finished_at"]


def test_run_agent_tracked_records_failure_with_zero_usage(monkeypatch):
    """A launch failure emits no usage -> a 'failed' run with zero tokens is
    still recorded, so the attempt stays visible."""
    monkeypatch.setattr(rt, "run_agent", lambda *a, **k: (False, "boom"))
    client = RunRecordingClient()
    ok, _ = rt.run_agent_tracked(_tracking_cfg(), client, {"id": 9}, "p",
                                 None, "implement", None)
    assert ok is False
    _, fields = client.runs[0]
    assert fields["phase"] == "implement"
    assert fields["status"] == "failed"
    assert fields["input_tokens"] == 0
    assert fields["cost_usd"] == 0.0


def test_run_agent_tracked_swallows_post_failure(monkeypatch):
    """A failing create_agent_run must never abort the phase."""
    monkeypatch.setattr(rt, "run_agent", lambda *a, **k: (True, "ok"))
    client = RunRecordingClient(raise_on_post=True)
    ok, text = rt.run_agent_tracked(_tracking_cfg(), client, {"id": 3}, "p",
                                    None, "review", None)
    assert (ok, text) == (True, "ok")  # phase result preserved despite POST error


def test_emit_token_usage_noop_without_collector():
    """Outside a tracked run (no contextvar set), _emit_token_usage just audits
    and doesn't raise."""
    rt._emit_token_usage(logging.getLogger("resolver"), "claude", "plan", {
        "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "cost_usd": 0.0, "model": "",
    })
