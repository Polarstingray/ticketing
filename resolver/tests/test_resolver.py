"""Unit tests for the resolver hardening + audit logging.

No network, no subprocesses, no real Claude — every external edge is stubbed.
"""
import json
import logging
import os
import tarfile
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import audit
import file_ticket as ft
import logs as logviewer
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


def test_implement_prompt_includes_file_hints(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "models.py").write_text("x = 1\n")
    (tmp_path / "config.py").write_text("y = 2\n")
    plan = ("Edit backend/models.py to add a column, then update config.py. "
            "Do not touch nonexistent/ghost.py.")
    prompt = rt.implement_prompt(
        {"id": 1, "title": "t", "description": "d"}, tmp_path, plan=plan)
    assert "Likely-relevant files" in prompt
    assert "- backend/models.py" in prompt
    assert "- config.py" in prompt
    # the hallucinated path is filtered out because it doesn't exist on disk
    assert "ghost.py" not in prompt.split("Likely-relevant files")[1]


def test_files_mentioned_in_plan_filters_to_existing(tmp_path):
    (tmp_path / "real.py").write_text("pass\n")
    found = rt._files_mentioned_in_plan(
        "Touch real.py and ./real.py again, but not missing.py", tmp_path)
    assert found == ["real.py"]  # deduped (incl. ./ prefix) and existence-filtered


def test_files_mentioned_in_plan_skips_absolute(tmp_path):
    (tmp_path / "real.py").write_text("pass\n")
    # The absolute path exists on disk but must NOT be offered as a hint — a hint has
    # to be worktree-relative so the implement agent stays inside the sandbox.
    plan = f"Edit {tmp_path / 'real.py'} and also real.py"
    assert rt._files_mentioned_in_plan(plan, tmp_path) == ["real.py"]


def test_implement_prompt_reanchors_absolute_plan_paths(tmp_path):
    main_repo = tmp_path / "ticketing"
    wt = tmp_path / "ticketing" / "resolver" / "work" / "ticket-7"
    plan = f"Edit {main_repo}/resolver/resolve_tickets.py to add the flag."
    prompt = rt.implement_prompt(
        {"id": 7, "title": "t", "description": "d"}, wt, plan=plan, main_repo=main_repo)
    assert f"{wt}/resolver/resolve_tickets.py" in prompt
    assert f"{main_repo}/resolver/resolve_tickets.py" not in prompt


def test_reanchor_respects_path_boundary(tmp_path):
    main_repo = Path("/home/u/repo")
    wt = Path("/wt/ticket-1")
    # "/home/u/repo/x" is rewritten; the sibling "/home/u/repo-backup/y" is left alone.
    text = "edit /home/u/repo/x.py but not /home/u/repo-backup/y.py"
    out = rt._reanchor(text, main_repo, wt)
    assert "/wt/ticket-1/x.py" in out
    assert "/home/u/repo-backup/y.py" in out


def test_implement_prompt_main_repo_optional(tmp_path):
    # Without main_repo, the plan text is passed through untouched (back-compat).
    plan = f"Edit {tmp_path}/resolver/x.py"
    prompt = rt.implement_prompt(
        {"id": 1, "title": "t", "description": "d"}, tmp_path, plan=plan)
    assert f"{tmp_path}/resolver/x.py" in prompt


# --- per-phase model selection ------------------------------------------
def test_model_for_falls_back_to_agent_model():
    cfg = SimpleNamespace(agent_model="base")
    assert rt.model_for(cfg, "plan") == "base"
    assert rt.model_for(cfg, "implement") == "base"
    assert rt.model_for(cfg, "review") == "base"


def test_model_for_picks_per_phase():
    cfg = SimpleNamespace(
        agent_model="base", agent_plan_model="cheap",
        agent_implement_model="strong", agent_review_model="")
    assert rt.model_for(cfg, "plan") == "cheap"
    assert rt.model_for(cfg, "implement") == "strong"
    assert rt.model_for(cfg, "review") == "base"  # blank override falls back


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
    fake_cfg.agent_fallback_model = ""
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


def test_opencode_event_accumulates_tokens():
    state = {}
    finish = {"type": "step_finish", "part": {
        "type": "step-finish", "reason": "stop",
        "tokens": {"input": 100, "output": 20, "cache": {"read": 5, "write": 3}},
        "cost": 0.01}}
    rt._opencode_event(logging.getLogger("resolver.octok"), json.dumps(finish), state)
    rt._opencode_event(logging.getLogger("resolver.octok"), json.dumps(finish), state)
    assert state["tokens"] == {"input": 200, "output": 40, "cache_read": 10, "cache_write": 6}
    assert state["cost"] == pytest.approx(0.02)


def test_claude_result_emits_token_usage():
    logger = logging.getLogger("resolver.toktest")
    logger.setLevel(logging.INFO)
    cap = _Capture()
    logger.handlers = [cap]
    logger.propagate = False
    rt._emit_token_usage(logger, "claude", "plan", {
        "input_tokens": 1234, "output_tokens": 56,
        "cache_read_tokens": 7, "cache_write_tokens": 8, "cost_usd": 0.042})
    rec = [r.audit for r in cap.records if getattr(r, "audit", {}).get("kind") == "token_usage"]
    assert len(rec) == 1
    assert rec[0]["agent"] == "claude"
    assert rec[0]["mode"] == "plan"
    assert rec[0]["input_tokens"] == 1234
    assert rec[0]["cost_usd"] == pytest.approx(0.042)


def test_emit_token_usage_defaults_missing_to_zero():
    logger = logging.getLogger("resolver.tokzero")
    logger.setLevel(logging.INFO)
    cap = _Capture()
    logger.handlers = [cap]
    logger.propagate = False
    rt._emit_token_usage(logger, "opencode", "implement", {})
    rec = [r.audit for r in cap.records if getattr(r, "audit", {}).get("kind") == "token_usage"][0]
    assert rec["input_tokens"] == 0
    assert rec["cost_usd"] == 0.0


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
    # opencode must be anchored to the worktree, else it roots in its global project.
    assert cmd[cmd.index("--dir") + 1] == str(tmp_path)
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


def test_run_opencode_no_output_is_failure_not_silent_success(tmp_path, fake_cfg, monkeypatch):
    # A swallowed provider error (e.g. Gemini 503): opencode emits only step_start,
    # makes no edits, and still exits 0. Must be reported as a failure so it isn't
    # misread downstream as "produced no code changes".
    _set_opencode_cfg(fake_cfg)
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    calls = []

    def only_step_start(cmd, cwd, timeout, log_path, on_line, *, category, label):
        calls.append(list(cmd))
        on_line(json.dumps({"type": "step_start", "part": {"type": "step-start"}}))
        return 0, False, None  # rc=0, not timed out, no launch error

    monkeypatch.setattr(rt, "_stream_subprocess", only_step_start)
    ok, msg = rt.run_opencode(fake_cfg, "do it", tmp_path, "implement", tmp_path / "i.log")
    assert ok is False
    assert "no output" in msg and "provider" in msg
    # swallowed-503 is retryable: with no fallback the primary is retried once.
    assert len(calls) == 2


def test_run_opencode_retries_then_escalates_to_fallback_model(tmp_path, fake_cfg, monkeypatch):
    # Transient failure on the primary -> retry primary -> escalate to the fallback
    # model, which then succeeds. One overloaded-model blip shouldn't burn the ticket.
    _set_opencode_cfg(fake_cfg)
    fake_cfg.agent_fallback_model = "google/gemini-2.5-pro"
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    models = []

    def stream(cmd, cwd, timeout, log_path, on_line, *, category, label):
        model = cmd[cmd.index("--model") + 1]
        models.append(model)
        if model == "google/gemini-2.5-pro":
            on_line(json.dumps({"type": "text", "part": {"text": "fixed it"}}))
            on_line(json.dumps({"type": "step_finish", "part": {"reason": "stop"}}))
        else:
            on_line(json.dumps({"type": "step_start", "part": {"type": "step-start"}}))
        return 0, False, None

    monkeypatch.setattr(rt, "_stream_subprocess", stream)
    ok, text = rt.run_opencode(fake_cfg, "do it", tmp_path, "implement", tmp_path / "i.log")
    assert ok is True and text == "fixed it"
    assert models == ["google/gemini-2.5-flash", "google/gemini-2.5-flash",
                      "google/gemini-2.5-pro"]


def test_run_opencode_success_does_not_retry(tmp_path, fake_cfg, monkeypatch):
    _set_opencode_cfg(fake_cfg)
    fake_cfg.agent_fallback_model = "google/gemini-2.5-pro"
    calls = []

    def stream(cmd, cwd, timeout, log_path, on_line, *, category, label):
        calls.append(1)
        on_line(json.dumps({"type": "text", "part": {"text": "ok"}}))
        on_line(json.dumps({"type": "step_finish", "part": {"reason": "stop"}}))
        return 0, False, None

    monkeypatch.setattr(rt, "_stream_subprocess", stream)
    ok, text = rt.run_opencode(fake_cfg, "do it", tmp_path, "implement", tmp_path / "i.log")
    assert ok is True and text == "ok" and len(calls) == 1


def test_run_opencode_non_retryable_error_returns_immediately(tmp_path, fake_cfg, monkeypatch):
    # An auth-style error is not transient: don't retry, don't escalate.
    _set_opencode_cfg(fake_cfg)
    fake_cfg.agent_fallback_model = "google/gemini-2.5-pro"
    calls = []

    def stream(cmd, cwd, timeout, log_path, on_line, *, category, label):
        calls.append(1)
        on_line(json.dumps({"type": "error", "error": {"name": "AuthError"}}))
        return 0, False, None

    monkeypatch.setattr(rt, "_stream_subprocess", stream)
    ok, msg = rt.run_opencode(fake_cfg, "do it", tmp_path, "implement", tmp_path / "i.log")
    assert ok is False and "AuthError" in msg and len(calls) == 1


def test_run_opencode_once_classifies_retryable():
    # The swallowed-503 shape (clean exit, no stop event, no text) and overload-named
    # errors are retryable; auth errors are not. Drive _run_opencode_once directly.
    assert rt._RETRYABLE_ERR.search("model overloaded, 503")
    assert rt._RETRYABLE_ERR.search("RESOURCE_EXHAUSTED")
    assert not rt._RETRYABLE_ERR.search("AuthError")
    assert not rt._RETRYABLE_ERR.search("permission denied")


def test_filed_tickets_in_log_parses_created_lines(tmp_path):
    log = tmp_path / "impl.log"
    # file_ticket.py output lands in the agent's captured tool result (here shown
    # inline + once JSON-escaped, as it appears in a stream-json transcript).
    log.write_text('blah\ncreated ticket #42: Review X\n'
                   '{"text":"... created ticket #42 ... created ticket #57 ..."}\n')
    assert rt.filed_tickets_in_log(log) == [42, 57]   # de-duped, in order


def test_filed_tickets_in_log_missing_file_is_empty(tmp_path):
    assert rt.filed_tickets_in_log(tmp_path / "nope.log") == []


# --- review mode ---------------------------------------------------------
def test_review_prompt_blocks_vs_explore_and_readonly():
    base = {"id": 1, "title": "Review install", "priority": "low", "description": "check"}
    with_blocks = {**base, "code_blocks": [
        {"filename": "a.sh", "language": "bash", "line_start": 1, "line_end": 3, "content": "x"}]}
    no_blocks = {**base, "code_blocks": []}
    assert "locations above" in rt.review_prompt(with_blocks, Path("/r"), False)
    assert "Explore the repository" in rt.review_prompt(no_blocks, Path("/r"), False)
    assert "READ-ONLY" in rt.review_prompt(no_blocks, Path("/r"), False)
    assert "apply your recommended fixes" in rt.review_prompt(no_blocks, Path("/r"), True)


def _review_ticket(**over):
    t = {"id": 50, "type": "code_review", "title": "Review the installer",
         "tags": ["repo:x"], "status": "open", "created_by": 9,
         "description": "review the installer", "priority": "low", "code_blocks": []}
    t.update(over)
    return t


def test_process_routes_code_review_to_review_not_plan(fake_cfg, monkeypatch):
    called = {}
    monkeypatch.setattr(rt, "do_review", lambda c, cl, t, r, want_fix: called.update(review=True, want_fix=want_fix))
    monkeypatch.setattr(rt, "do_plan", lambda *a, **k: called.update(plan=True))
    rt.process(fake_cfg, FakeClient(), _review_ticket(), dry_run=False)
    assert called.get("review") and "plan" not in called
    assert called.get("want_fix") is False


def test_process_code_review_fix_tag_sets_want_fix(fake_cfg, monkeypatch):
    called = {}
    monkeypatch.setattr(rt, "do_review", lambda c, cl, t, r, want_fix: called.update(want_fix=want_fix))
    rt.process(fake_cfg, FakeClient(), _review_ticket(tags=["repo:x", "fix"]), dry_run=False)
    assert called.get("want_fix") is True


def test_process_task_ticket_still_plans(fake_cfg, monkeypatch):
    called = {}
    monkeypatch.setattr(rt, "do_plan", lambda c, cl, t, r, notes: called.update(plan=True))
    monkeypatch.setattr(rt, "do_review", lambda *a, **k: called.update(review=True))
    rt.process(fake_cfg, FakeClient(), _review_ticket(type="task"), dry_run=False)
    assert called.get("plan") and "review" not in called


def test_process_skips_already_reviewed_unless_review_cmd(fake_cfg, monkeypatch):
    called = {}
    monkeypatch.setattr(rt, "do_review", lambda *a, **k: called.update(review=True))
    reviewed = [{"author": BOT, "body": f"{rt.REVIEW_MARKER} done"}]
    rt.process(fake_cfg, FakeClient(reviewed), _review_ticket(status="in_review"), dry_run=False)
    assert "review" not in called  # already reviewed, no /review -> skip

    rt.process(fake_cfg, FakeClient(reviewed + [{"author": 9, "body": "/review"}]),
               _review_ticket(), dry_run=False)
    assert called.get("review")  # /review forces a re-review


def test_process_reviewing_tag_retries_review(fake_cfg, monkeypatch):
    called = {}
    monkeypatch.setattr(rt, "do_review", lambda *a, **k: called.update(review=True))
    rt.process(fake_cfg, FakeClient(), _review_ticket(tags=["repo:x", "claude:reviewing"]), dry_run=False)
    assert called.get("review")


def test_do_review_findings_only_posts_and_hands_back(fake_cfg, monkeypatch):
    monkeypatch.setattr(rt, "run_agent", lambda cfg, p, cwd, mode, log: (True, "FINDINGS"))
    client = FakeClient()
    rt.do_review(fake_cfg, client, _review_ticket(), fake_cfg.resolve_repo("x"), want_fix=False)
    assert any(rt.REVIEW_MARKER in b and "FINDINGS" in b for _, b in client.comments_added)
    last = client.updates[-1]
    assert last["status"] == "in_review" and last["assigned_to"] == 9
    assert all(not t.startswith("claude:") for t in last["tags"])


def test_do_review_with_fix_routes_to_plan_gate(fake_cfg, monkeypatch):
    monkeypatch.setattr(rt, "run_agent", lambda *a, **k: (True, "FINDINGS"))
    client = FakeClient()
    rt.do_review(fake_cfg, client, _review_ticket(tags=["repo:x", "fix"]),
                 fake_cfg.resolve_repo("x"), want_fix=True)
    assert any(rt.PLAN_MARKER in b for _, b in client.comments_added)
    last = client.updates[-1]
    assert rt.TAG_AWAIT_PLAN in last["tags"] and last["status"] == "in_review"


def test_do_review_fix_plus_dangerous_implements_directly(fake_cfg, monkeypatch):
    monkeypatch.setattr(rt, "run_agent", lambda *a, **k: (True, "FINDINGS"))
    impl = {}
    monkeypatch.setattr(rt, "do_implement", lambda c, cl, t, r, plan=None, **k: impl.update(plan=plan))
    rt.do_review(fake_cfg, FakeClient(), _review_ticket(tags=["repo:x", "fix", "dangerous"]),
                 fake_cfg.resolve_repo("x"), want_fix=True)
    assert impl.get("plan") == "FINDINGS"


def test_do_review_failure_is_reported(fake_cfg, monkeypatch):
    monkeypatch.setattr(rt, "run_agent", lambda *a, **k: (False, "boom"))
    client = FakeClient()
    rt.do_review(fake_cfg, client, _review_ticket(), fake_cfg.resolve_repo("x"), want_fix=False)
    assert any(rt.FAIL_MARKER in b for _, b in client.comments_added)


def test_run_opencode_review_uses_readonly_plan_agent(tmp_path, fake_cfg, monkeypatch):
    _set_opencode_cfg(fake_cfg)
    captured = {}

    def fake_stream(cmd, cwd, timeout, log_path, on_line, *, category, label):
        captured["cmd"] = list(cmd)
        captured["label"] = label
        on_line(json.dumps({"type": "text", "part": {"text": "the review"}}))
        on_line(json.dumps({"type": "step_finish", "part": {"reason": "stop"}}))
        return 0, False, None

    monkeypatch.setattr(rt, "_stream_subprocess", fake_stream)
    ok, text = rt.run_opencode(fake_cfg, "review it", tmp_path, "review", tmp_path / "r.log")
    assert ok is True and text == "the review"
    cmd = captured["cmd"]
    assert cmd[cmd.index("--agent") + 1] == "plan"          # read-only agent
    assert "--dangerously-skip-permissions" not in cmd       # cannot edit
    assert captured["label"] == "review"


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


# --- log lifecycle (archive / prune / discard / rotate) ------------------
def _logcfg(tmp_path, **over):
    ns = SimpleNamespace(
        logs_dir=tmp_path,
        log_archive_after_days=1,
        log_retention_days=14,
        cron_log=None,
        cron_log_max_bytes=5_000_000,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_archive_old_logs_batches_finished_days(tmp_path):
    cfg = _logcfg(tmp_path)
    old_sweep = tmp_path / "sweep-20200101-000000.log"
    old_sweep.write_text("old sweep")
    old_ticket = tmp_path / "ticket-42-implement-20200101-000000.log"
    old_ticket.write_text("impl log")
    for p in (old_sweep, old_ticket):
        _age(p, 3)
    today = tmp_path / "ticket-43-plan-20990101-000000.log"
    today.write_text("today")  # mtime = now, must be left loose

    assert audit.archive_old_logs(cfg) == 1
    day = date.fromtimestamp(time.time() - 3 * 86400).isoformat()
    tarball = tmp_path / "archive" / f"{day}.tar.gz"
    assert tarball.exists()
    with tarfile.open(tarball) as tar:
        assert sorted(m.name for m in tar.getmembers()) == [
            "sweep-20200101-000000.log",
            "ticket-42-implement-20200101-000000.log",
        ]
    assert not old_sweep.exists() and not old_ticket.exists()
    assert today.exists()  # today's log untouched


def test_archive_skips_when_nothing_old(tmp_path):
    cfg = _logcfg(tmp_path)
    (tmp_path / "ticket-1-plan-20990101-000000.log").write_text("fresh")
    assert audit.archive_old_logs(cfg) == 0
    assert not (tmp_path / "archive").exists() or not list((tmp_path / "archive").glob("*.tar.gz"))


def test_prune_old_logs_deletes_aged_archives(tmp_path):
    cfg = _logcfg(tmp_path, log_retention_days=14)
    arch = tmp_path / "archive"
    arch.mkdir()
    old_tar = arch / "2020-01-01.tar.gz"
    old_tar.write_bytes(b"x")
    _age(old_tar, 30)
    fresh_tar = arch / "2099-01-01.tar.gz"
    fresh_tar.write_bytes(b"y")
    assert audit.prune_old_logs(cfg) == 1
    assert not old_tar.exists()
    assert fresh_tar.exists()


def test_discard_sweep_logs_removes_pair(tmp_path):
    cfg = _logcfg(tmp_path)
    sid = "20260608-120000"
    (tmp_path / f"sweep-{sid}.log").write_text("start")
    (tmp_path / f"audit-{sid}.jsonl").write_text("{}")
    assert audit.discard_sweep_logs(cfg, sid) == 2
    assert not (tmp_path / f"sweep-{sid}.log").exists()
    assert not (tmp_path / f"audit-{sid}.jsonl").exists()


def test_rotate_cron_log_only_when_over_cap(tmp_path):
    cron = tmp_path / "cron.log"
    cron.write_text("x" * 100)
    assert audit.rotate_cron_log(_logcfg(tmp_path, cron_log=cron, cron_log_max_bytes=10)) is True
    assert (tmp_path / "cron.log.1").exists()
    assert not cron.exists()

    cron.write_text("tiny")
    assert audit.rotate_cron_log(
        _logcfg(tmp_path, cron_log=cron, cron_log_max_bytes=10_000)) is False
    assert cron.exists()


def test_logs_collect_picks_newest_run(tmp_path):
    (tmp_path / "ticket-42-implement-20260101-000000.log").write_text("first")
    (tmp_path / "ticket-42-implement-20260102-000000.log").write_text("second")
    (tmp_path / "ticket-42-plan-20260101-000000.log").write_text("planlog")
    refs = logviewer.collect_logs(tmp_path, ticket=42, phase="implement")
    assert [r.ts for r in refs] == ["20260102-000000", "20260101-000000"]
    assert logviewer.read_log(refs[0]) == "second"


def test_logs_reads_from_archive(tmp_path):
    arch = tmp_path / "archive"
    arch.mkdir()
    member = tmp_path / "ticket-99-implement-20260101-010101.log"
    member.write_text("archived body")
    with tarfile.open(arch / "2026-01-01.tar.gz", "w:gz") as tar:
        tar.add(member, arcname=member.name)
    member.unlink()
    refs = logviewer.collect_logs(tmp_path, ticket=99)
    assert len(refs) == 1
    assert refs[0].archive == arch / "2026-01-01.tar.gz"
    assert logviewer.read_log(refs[0]) == "archived body"


# --- filing tickets (file_ticket.py) -------------------------------------
def test_create_ticket_posts_to_tickets_endpoint():
    c = _client([FakeResp(201, {"id": 7, "title": "t"})])
    out = c.create_ticket(type="task", title="t", priority="low")
    assert out == {"id": 7, "title": "t"}
    assert c.session.calls == 1


def _args(**over):
    base = dict(type="code_review", title="t", description="", priority="medium",
               tag=None, assign=None, code_block=None, root=".")
    base.update(over)
    return SimpleNamespace(**base)


def test_parse_code_block_reads_exact_lines(tmp_path):
    (tmp_path / "a.py").write_text("L1\nL2\nL3\nL4\n")
    block = ft.parse_code_block("a.py:python:2-3", tmp_path)
    assert block == {
        "filename": "a.py", "language": "python",
        "line_start": 2, "line_end": 3, "content": "L2\nL3",
    }


def test_parse_code_block_single_line(tmp_path):
    (tmp_path / "a.py").write_text("only\n")
    block = ft.parse_code_block("a.py:python:1", tmp_path)
    assert block["line_start"] == block["line_end"] == 1
    assert block["content"] == "only"


@pytest.mark.parametrize("spec", [
    "a.py:python",          # no range
    "a.py:2-3",             # no language
    "a.py:python:3-1",      # end before start
    "a.py:python:0-2",      # start < 1
    "a.py:python:x-2",      # non-numeric
    "a.py:python:1-99",     # out of range
])
def test_parse_code_block_rejects_bad_specs(tmp_path, spec):
    (tmp_path / "a.py").write_text("one\ntwo\n")
    with pytest.raises(ValueError):
        ft.parse_code_block(spec, tmp_path)


def test_parse_code_block_missing_file(tmp_path):
    with pytest.raises(ValueError):
        ft.parse_code_block("nope.py:python:1-2", tmp_path)


def test_build_payload_assembles_fields(tmp_path):
    (tmp_path / "a.py").write_text("x\ny\nz\n")
    args = _args(tag=["backend", "auth"], assign=5,
                 code_block=["a.py:python:1-2"], root=str(tmp_path))
    payload = ft.build_payload(args)
    assert payload["type"] == "code_review"
    assert payload["tags"] == ["backend", "auth"]
    assert payload["assigned_to"] == 5
    assert payload["code_blocks"][0]["content"] == "x\ny"


def test_build_payload_blank_title_rejected():
    with pytest.raises(ValueError):
        ft.build_payload(_args(title="   "))


def test_build_payload_code_block_requires_code_review(tmp_path):
    (tmp_path / "a.py").write_text("x\n")
    args = _args(type="task", code_block=["a.py:python:1"], root=str(tmp_path))
    with pytest.raises(ValueError):
        ft.build_payload(args)


# --- /ticket directive (resolver-parsed) ---------------------------------
class DirectiveClient:
    """Records create_ticket + add_comment so directive tests can assert."""

    def __init__(self, comments=None):
        self._comments = comments or []
        self.created: list[dict] = []
        self.comments_added: list[tuple[int, str]] = []

    def list_comments(self, tid):
        return self._comments

    def create_ticket(self, **fields):
        self.created.append(fields)
        return {"id": 100 + len(self.created), "title": fields.get("title")}

    def add_comment(self, tid, body):
        self.comments_added.append((tid, body))
        return {"id": 1}


def test_collect_directives_body_and_comments_ignores_bot_and_noise():
    ticket = {"id": 1, "created_by": 9,
              "description": "fix it\n/ticket task \"Add index\" --tag backend"}
    comments = [
        {"author": 9, "body": "/ticket code_review \"Review\""},
        {"author": BOT, "body": "/ticket task \"bot wrote this\""},  # ignored
        {"author": 9, "body": "plain note, no directive"},
    ]
    ds = rt.collect_directives(ticket, comments, bot_id=BOT)
    assert [d["author"] for d in ds] == [9, 9]
    assert ds[0]["args"].startswith('task "Add index"')


def test_collect_directives_dedups_identical_lines_in_one_sweep():
    ticket = {"id": 1, "created_by": 9, "description": '/ticket task "dup"'}
    comments = [{"author": 9, "body": '/ticket task "dup"'}]
    ds = rt.collect_directives(ticket, comments, bot_id=BOT)
    assert len(ds) == 1


def test_directive_payload_defaults_to_author_and_assign_overrides(tmp_path):
    body = {"key": "k", "line": "x", "author": 9,
            "args": 'task "Add index" --priority high --tag backend'}
    p = rt.directive_payload(body, tmp_path)
    assert p["assigned_to"] == 9 and p["priority"] == "high" and p["tags"] == ["backend"]

    explicit = {"key": "k", "line": "x", "author": 9,
                "args": 'task "t" --assign 3'}
    assert rt.directive_payload(explicit, tmp_path)["assigned_to"] == 3


@pytest.mark.parametrize("args", [
    'bug "x"',                       # bad type
    'task',                          # missing title
    'task "x" --priority huge',      # bad priority
    'task "x" --code-block a.py:python:1',  # code_block on a task
    'task "unbalanced',              # shlex error
])
def test_directive_payload_rejects_bad(tmp_path, args):
    d = {"key": "k", "line": "/ticket " + args, "author": 9, "args": args}
    with pytest.raises(rt._DirectiveError):
        rt.directive_payload(d, tmp_path)


def test_handle_directives_files_once_and_marks(fake_cfg, tmp_path):
    client = DirectiveClient()
    ticket = {"id": 7, "created_by": 9, "description": '/ticket task "Add index" --tag backend'}
    rt.handle_ticket_directives(fake_cfg, client, ticket, client.list_comments(7), tmp_path, dry_run=False)
    assert len(client.created) == 1
    assert client.created[0]["type"] == "task" and client.created[0]["assigned_to"] == 9
    # exactly one marker comment, carrying the directive key for next-sweep dedup
    assert len(client.comments_added) == 1
    body = client.comments_added[0][1]
    assert rt.FILED_MARKER in body and "[key:" in body


def test_handle_directives_dedup_skips_already_filed(fake_cfg, tmp_path):
    line = '/ticket task "Add index"'
    key = rt.directive_key(line)
    client = DirectiveClient(comments=[
        {"author": BOT, "body": f"{rt.FILED_MARKER}\n\n- #101 — task \"Add index\"  [key:{key}]"},
    ])
    ticket = {"id": 7, "created_by": 9, "description": line}
    rt.handle_ticket_directives(fake_cfg, client, ticket, client.list_comments(7), tmp_path, dry_run=False)
    assert client.created == [] and client.comments_added == []


def test_handle_directives_bad_directive_reported_not_raised(fake_cfg, tmp_path):
    client = DirectiveClient()
    ticket = {"id": 7, "created_by": 9, "description": '/ticket bogus "x"'}
    rt.handle_ticket_directives(fake_cfg, client, ticket, client.list_comments(7), tmp_path, dry_run=False)
    assert client.created == []
    assert len(client.comments_added) == 1
    assert "error:" in client.comments_added[0][1]


def test_handle_directives_dry_run_files_nothing(fake_cfg, tmp_path):
    client = DirectiveClient()
    ticket = {"id": 7, "created_by": 9, "description": '/ticket task "Add index"'}
    rt.handle_ticket_directives(fake_cfg, client, ticket, client.list_comments(7), tmp_path, dry_run=True)
    assert client.created == [] and client.comments_added == []


def test_directive_assign_username_gives_actionable_error(tmp_path):
    d = {"key": "k", "line": "x", "author": 9,
         "args": 'code_review "sha256 verification" --assign admin'}
    with pytest.raises(rt._DirectiveError) as exc:
        rt.directive_payload(d, tmp_path)
    assert "numeric user id" in str(exc.value) and "omit --assign" in str(exc.value)


@pytest.mark.parametrize("desc,expected", [
    ('/ticket task "x"', True),
    ('\n/ticket task "x"\n  ', True),
    ('/ticket task "a"\n/ticket task "b"', True),
    ('do real work\n/ticket task "x"', False),  # has substantive content
    ('', False),                                  # empty body
    ('just a normal task', False),                # no directive
])
def test_body_is_directive_only(desc, expected):
    assert rt.body_is_directive_only({"description": desc}) is expected


class FakeClientWithCreate(FakeClient):
    def __init__(self, comments=None):
        super().__init__(comments)
        self.created: list[dict] = []

    def create_ticket(self, **fields):
        self.created.append(fields)
        return {"id": 200 + len(self.created), "title": fields.get("title")}


def test_process_directive_only_files_and_hands_back_without_planning(fake_cfg):
    client = FakeClientWithCreate()
    ticket = {"id": 7, "tags": ["repo:x"], "status": "open", "created_by": 9,
              "description": '/ticket code_review "sha256 verification"'}

    rt.process(fake_cfg, client, ticket, dry_run=False)

    # filed the directive once...
    assert len(client.created) == 1 and client.created[0]["type"] == "code_review"
    # ...handed the host ticket back to the author, no claude:* tags, never planned
    last = client.updates[-1]
    assert last["status"] == "in_review" and last["assigned_to"] == 9
    assert all(not t.startswith("claude:") for t in last["tags"])
    assert not any("planning" in body for _, body in client.comments_added)


def test_process_directive_only_dry_run_changes_nothing(fake_cfg):
    client = FakeClientWithCreate()
    ticket = {"id": 7, "tags": ["repo:x"], "status": "open", "created_by": 9,
              "description": '/ticket task "x"'}
    rt.process(fake_cfg, client, ticket, dry_run=True)
    assert client.created == [] and client.updates == [] and client.comments_added == []
