"""Unit tests for the resolver hardening + audit logging.

No network, no subprocesses, no real Claude — every external edge is stubbed.
"""
import json
import logging
import os

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

    tools = [r.audit for r in cap.records if getattr(r, "audit", {}).get("kind") == "agent_tool"]
    assert len(tools) == 1
    assert tools[0]["tool"] == "Bash"
    assert tools[0]["agent"] == "claude"
    assert "pytest -q" in tools[0]["input_summary"]
    assert result.get("result") == "all done"


def test_summarize_tool_use_shapes():
    assert rt.summarize_tool_use("Read", {"file_path": "/a/b.py"}) == "/a/b.py"
    assert rt.summarize_tool_use("Write", {"file_path": "/x.py"}) == "/x.py"
    assert rt.summarize_tool_use("Grep", {"pattern": "foo", "path": "src"}) == "foo in src"


def test_run_claude_handles_missing_binary(tmp_path, fake_cfg):
    fake_cfg.agent_bin = "definitely-not-a-real-binary-xyz"
    fake_cfg.agent_model = ""
    fake_cfg.implement_tools = "Read"
    fake_cfg.agent_timeout = 5
    fake_cfg.agent_implement_timeout = 5
    log_path = tmp_path / "out.log"
    ok, msg = rt.run_claude(fake_cfg, "hi", tmp_path, "plan", log_path)
    assert ok is False
    assert "definitely-not-a-real-binary-xyz" in msg


# --- opencode runner -----------------------------------------------------
def _set_opencode_cfg(fake_cfg):
    fake_cfg.agent_bin = "opencode"
    fake_cfg.agent_model = "google/gemini-2.5-flash"
    fake_cfg.agent_timeout = 5
    fake_cfg.agent_implement_timeout = 5
    fake_cfg.opencode_plan_agent = "plan"
    fake_cfg.opencode_build_agent = "build"
    return fake_cfg


def test_opencode_event_parses_tool_and_final_text():
    logger = logging.getLogger("resolver.opencodetest")
    logger.setLevel(logging.DEBUG)
    cap = _Capture()
    logger.handlers = [cap]
    logger.propagate = False

    # Event shapes verified against a live opencode 1.16.2 `run --format json`:
    # tool_use carries part.tool + part.state.input; step_finish's stop reason is
    # on part.reason (NOT the top level).
    state = {}
    rt._opencode_event(logger, json.dumps({"type": "tool_use", "part": {
        "type": "tool", "tool": "bash",
        "state": {"status": "completed", "input": {"command": "pytest -q"}}}}), state)
    rt._opencode_event(logger, json.dumps({"type": "step_start", "part": {"type": "step-start"}}), state)
    rt._opencode_event(logger, json.dumps({"type": "text", "part": {"type": "text", "text": "all "}}), state)
    rt._opencode_event(logger, json.dumps({"type": "text", "part": {"type": "text", "text": "done"}}), state)
    rt._opencode_event(logger, json.dumps({"type": "step_finish",
        "part": {"type": "step-finish", "reason": "stop"}}), state)

    tools = [r.audit for r in cap.records if getattr(r, "audit", {}).get("kind") == "agent_tool"]
    assert len(tools) == 1
    assert tools[0]["agent"] == "opencode"
    assert tools[0]["tool"] == "bash"
    assert "pytest -q" in tools[0]["input_summary"]
    # the final answer is the text from the step that ends with reason=stop
    assert state["final"] == "all done"
    assert state["stopped"] is True


def test_opencode_event_records_error():
    state = {}
    rt._opencode_event(logging.getLogger("resolver.ocerr"),
                       json.dumps({"type": "error", "error": {"name": "ProviderError"}}), state)
    assert state["error"]["name"] == "ProviderError"


def test_summarize_opencode_tool_shapes():
    assert rt._summarize_opencode_tool("read", {"filePath": "/a/b.py"}) == "/a/b.py"
    assert rt._summarize_opencode_tool("bash", {"command": "ls -la"}) == "ls -la"
    assert rt._summarize_opencode_tool("grep", {"pattern": "foo", "path": "src"}) == "foo in src"


def test_run_opencode_plan_is_readonly_implement_can_edit(tmp_path, fake_cfg, monkeypatch):
    _set_opencode_cfg(fake_cfg)
    captured = {}

    def fake_stream(cmd, cwd, timeout, log_path, on_line, *, category, label):
        captured["cmd"] = list(cmd)
        captured["category"] = category
        captured["label"] = label
        on_line(json.dumps({"type": "text", "part": {"text": "the plan"}}))
        on_line(json.dumps({"type": "step_finish", "part": {"reason": "stop"}}))
        return 0, False, None

    monkeypatch.setattr(rt, "_stream_subprocess", fake_stream)

    # plan: the read-only `plan` agent, and crucially NO skip-permissions flag.
    ok, text = rt.run_opencode(fake_cfg, "do it", tmp_path, "plan", tmp_path / "p.log")
    assert ok is True and text == "the plan"
    cmd = captured["cmd"]
    assert cmd[:4] == ["opencode", "run", "do it", "--format"]
    assert "--model" in cmd and "google/gemini-2.5-flash" in cmd
    assert cmd[cmd.index("--agent") + 1] == "plan"
    assert "--dangerously-skip-permissions" not in cmd
    assert captured["category"] == "opencode" and captured["label"] == "plan"

    # implement: the unrestricted `build` agent, edits auto-approved.
    rt.run_opencode(fake_cfg, "do it", tmp_path, "implement", tmp_path / "i.log")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--agent") + 1] == "build"
    assert "--dangerously-skip-permissions" in cmd


def test_run_opencode_handles_missing_binary(tmp_path, fake_cfg):
    _set_opencode_cfg(fake_cfg)
    fake_cfg.agent_bin = "definitely-not-a-real-binary-xyz"
    fake_cfg.agent_model = ""
    ok, msg = rt.run_opencode(fake_cfg, "hi", tmp_path, "plan", tmp_path / "o.log")
    assert ok is False
    assert "definitely-not-a-real-binary-xyz" in msg


def test_opencode_runner_registered_and_unknown_fails():
    import agents
    runner = agents.get_runner("opencode")
    assert runner.name == "opencode"
    with pytest.raises(SystemExit):
        agents.get_runner("nope-not-a-registered-agent")


def test_resolver_env_file_selects_alternate_identity(tmp_path, monkeypatch):
    import config
    proj = tmp_path / "proj"
    proj.mkdir()
    envf = tmp_path / "alt.env"
    envf.write_text(
        "STINGRAY_URL=http://x/api\n"
        "STINGRAY_API_KEY=sk_test_unit_key_alt_000000\n"
        "RESOLVER_BOT_USER_ID=3\n"
        "RESOLVER_AGENT=opencode\n"
        f"PROJECTS_ROOT={proj}\n"
        "AGENT_BIN=opencode\n"
        "AGENT_MODEL=google/gemini-2.5-flash\n"
    )
    monkeypatch.setenv("RESOLVER_ENV_FILE", str(envf))
    saved = dict(os.environ)  # _load_env_file mutates os.environ; restore after
    try:
        cfg = config.Config.load()
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert cfg.agent == "opencode"
    assert cfg.bot_user_id == 3
    assert cfg.agent_bin == "opencode"
    assert cfg.agent_model == "google/gemini-2.5-flash"


def test_env_prefers_agent_then_falls_back_to_claude(monkeypatch):
    import config
    monkeypatch.delenv("AGENT_BIN", raising=False)
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    assert config._env("AGENT_BIN", "CLAUDE_BIN", default="claude") == "claude"
    monkeypatch.setenv("CLAUDE_BIN", "claude")
    assert config._env("AGENT_BIN", "CLAUDE_BIN", default="x") == "claude"
    monkeypatch.setenv("AGENT_BIN", "opencode")
    assert config._env("AGENT_BIN", "CLAUDE_BIN", default="x") == "opencode"
