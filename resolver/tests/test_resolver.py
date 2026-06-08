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


# --- /ticket directive: parsing -----------------------------------------
def test_parse_ticket_directive_full():
    spec = rt.parse_ticket_directive(
        [{"author": 9, "body": "please file\n/ticket review assign:5 priority:high "
          "tags:backend,auth title:Review the auth refactor"}], BOT)
    assert spec["kind"] == "code_review"
    assert spec["assignee_id"] == 5
    assert spec["priority"] == "high"
    assert spec["tags"] == ["backend", "auth"]
    assert spec["title"] == "Review the auth refactor"


def test_parse_ticket_directive_absent():
    assert rt.parse_ticket_directive([{"author": 9, "body": "/approve"}], BOT) is None
    assert rt.parse_ticket_directive([], BOT) is None
    # A bot comment that happens to mention /ticket must be ignored.
    assert rt.parse_ticket_directive([{"author": BOT, "body": "/ticket review"}], BOT) is None


def test_parse_ticket_directive_bare_assign_and_task():
    spec = rt.parse_ticket_directive(
        [{"author": 9, "body": "/ticket task +assign look at this"}], BOT)
    assert spec["kind"] == "task"
    assert spec["assign_fallback"] is True
    assert spec["assignee_id"] is None
    assert "look at this" in spec["description"]


def test_parse_ticket_directive_rejects_bot_assignee():
    spec = rt.parse_ticket_directive(
        [{"author": 9, "body": f"/ticket assign:{BOT}"}], BOT)
    assert spec["assignee_id"] is None
    assert spec["assign_fallback"] is True  # dropped to the fallback, never the bot


def test_parse_ticket_directive_prefers_latest_human():
    comments = [
        {"author": 9, "body": "/ticket review tags:old"},
        {"author": BOT, "body": "bot noise"},
        {"author": 9, "body": "/ticket review tags:new"},
    ]
    assert rt.parse_ticket_directive(comments, BOT)["tags"] == ["new"]


def test_resolve_assignee_fallbacks(fake_cfg):
    ticket = {"created_by": 7}
    bare = {"assign_fallback": True, "assignee_id": None}
    assert rt._resolve_assignee(bare, fake_cfg, ticket) == 7  # creator fallback
    fake_cfg.default_reviewer_id = 42
    assert rt._resolve_assignee(bare, fake_cfg, ticket) == 42  # configured reviewer wins
    none_spec = {"assignee_id": None, "assign_fallback": False}
    assert rt._resolve_assignee(none_spec, fake_cfg, ticket) is None


# --- /ticket directive: diff -> code_blocks -----------------------------
MULTI_DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 import os
+import sys
 x = 1
@@ -10,2 +11,3 @@
 y = 2
+z = 3
 w = 4
diff --git a/b.js b/b.js
--- a/b.js
+++ b/b.js
@@ -5,1 +5,2 @@
 const a = 1;
+const b = 2;
"""


def test_diff_to_code_blocks_multi_file_hunk():
    blocks = rt.diff_to_code_blocks(MULTI_DIFF)
    assert len(blocks) == 3
    a1, a2, b1 = blocks
    assert a1["filename"] == "a.py" and a1["language"] == "python"
    assert a1["line_start"] == 1 and a1["line_end"] == 3
    assert a1["content"] == "import os\nimport sys\nx = 1"  # removed lines dropped
    assert a2["line_start"] == 11 and a2["line_end"] == 13
    assert b1["filename"] == "b.js" and b1["language"] == "javascript"
    assert b1["line_start"] == 5 and b1["line_end"] == 6


def test_diff_to_code_blocks_respects_cap():
    chunks = ["diff --git a/x.py b/x.py", "--- a/x.py", "+++ b/x.py"]
    for i in range(30):
        chunks += [f"@@ -{i+1},1 +{i+1},1 @@", f"+line{i}"]
    assert len(rt.diff_to_code_blocks("\n".join(chunks), max_blocks=5)) == 5


# --- /ticket directive: publish files a follow-up -----------------------
SAMPLE_DIFF = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 import os
+import sys
 print(os.getcwd())
"""


def _publish_run(calls):
    def fake_run(cmd, cwd=None, timeout=None):
        calls.append(cmd)
        if "push" in cmd:
            return 0, ""
        if cmd and cmd[0] == "gh":
            return 0, "https://github.com/x/y/pull/42"
        if "diff" in cmd:
            return 0, SAMPLE_DIFF
        return 0, ""
    return fake_run


def _publish(fake_cfg, client, ticket, spec):
    rt.publish(fake_cfg, client, ticket, fake_cfg.logs_dir / "repo",
               fake_cfg.logs_dir / "wt", "claude/ticket-5", "HEAD", "main",
               "did stuff", "1 file", origin=True, pr_ok=True, spec=spec)


def test_publish_with_spec_files_followup(fake_cfg, monkeypatch):
    monkeypatch.setattr(rt, "run", _publish_run([]))
    client = FakeClient()
    ticket = {"id": 5, "tags": [], "status": "in_review", "created_by": 3,
              "title": "Fix the bug", "priority": "high"}
    spec = rt.parse_ticket_directive(
        [{"author": 9, "body": "/ticket review assign:5 tags:auth"}], BOT)

    _publish(fake_cfg, client, ticket, spec)

    assert len(client.created) == 1
    created = client.created[0]
    assert created["type"] == "code_review"
    assert created["assigned_to"] == 5 and created["assigned_to"] != BOT
    assert created["code_blocks"] and created["code_blocks"][0]["filename"] == "foo.py"
    assert "auth" in created["tags"] and "resolver" in created["tags"]
    assert any(rt.FILED_MARKER in body for _, body in client.comments_added)
    assert rt.TAG_FILED in client.updates[-1]["tags"]  # guards a later rework


def test_publish_does_not_refile_when_already_filed(fake_cfg, monkeypatch):
    monkeypatch.setattr(rt, "run", _publish_run([]))
    client = FakeClient()
    ticket = {"id": 5, "tags": [rt.TAG_FILED], "status": "in_review",
              "created_by": 3, "title": "t", "priority": "low"}
    spec = rt.parse_ticket_directive([{"author": 9, "body": "/ticket"}], BOT)
    _publish(fake_cfg, client, ticket, spec)
    assert client.created == []


def test_publish_without_spec_files_nothing(fake_cfg, monkeypatch):
    monkeypatch.setattr(rt, "run", _publish_run([]))
    client = FakeClient()
    ticket = {"id": 5, "tags": [], "status": "in_review", "created_by": 3,
              "title": "t", "priority": "low"}
    _publish(fake_cfg, client, ticket, None)
    assert client.created == []
    assert not any(rt.FILED_MARKER in b for _, b in client.comments_added)


def test_patch_fallback_files_nothing(fake_cfg, monkeypatch):
    fake_cfg.patch_fallback = True
    monkeypatch.setattr(rt, "run", _publish_run([]))
    client = FakeClient()
    ticket = {"id": 5, "tags": [], "status": "in_review", "created_by": 3,
              "title": "t", "priority": "low"}
    spec = rt.parse_ticket_directive([{"author": 9, "body": "/ticket"}], BOT)
    _publish(fake_cfg, client, ticket, spec)
    assert client.created == []


# --- /ticket directive: standalone filing after the PR is up ------------
def _branch_env(monkeypatch, *, ref_ok=True):
    """Stub the git plumbing file_followup_from_branch reaches for: a published
    branch whose diff is SAMPLE_DIFF, branching off HEAD."""
    def fake_run(cmd, cwd=None, timeout=None):
        return (0, SAMPLE_DIFF) if "diff" in cmd else (0, "")
    monkeypatch.setattr(rt, "run", fake_run)
    monkeypatch.setattr(rt, "has_origin", lambda repo: True)
    monkeypatch.setattr(rt, "resolve_base", lambda repo: ("HEAD", "main"))
    monkeypatch.setattr(rt, "ref_exists", lambda repo, ref: ref_ok)


def test_process_files_followup_from_branch_after_pr(fake_cfg, monkeypatch):
    _branch_env(monkeypatch)
    comments = [
        {"author": BOT, "body": f"{rt.IMPL_MARKER} — http://pr/1\n\ndid the thing"},
        {"author": 9, "body": "/ticket review tags:auth"},
    ]
    client = FakeClient(comments)
    ticket = {"id": 5, "tags": [rt.TAG_AWAIT_PR], "status": "in_review",
              "created_by": 9, "title": "Fix the bug", "priority": "high"}

    rt.process(fake_cfg, client, ticket, dry_run=False)

    assert len(client.created) == 1
    created = client.created[0]
    assert created["type"] == "code_review"
    assert created["code_blocks"] and created["code_blocks"][0]["filename"] == "foo.py"
    assert "auth" in created["tags"] and "resolver" in created["tags"]
    assert "did the thing" in created["description"]  # defaulted to the impl summary
    assert any(rt.FILED_MARKER in body for _, body in client.comments_added)
    assert rt.TAG_FILED in client.updates[-1]["tags"]


def test_process_followup_idempotent_when_already_filed(fake_cfg, monkeypatch):
    _branch_env(monkeypatch)
    comments = [{"author": 9, "body": "/ticket"}]
    client = FakeClient(comments)
    ticket = {"id": 5, "tags": [rt.TAG_AWAIT_PR, rt.TAG_FILED], "status": "in_review",
              "created_by": 9, "title": "t", "priority": "low"}

    rt.process(fake_cfg, client, ticket, dry_run=False)

    assert client.created == []
    assert any("already filed" in body for _, body in client.comments_added)
    assert rt.TAG_FILED in client.updates[-1]["tags"]  # not dropped


def test_process_followup_no_branch_is_not_silent(fake_cfg, monkeypatch):
    _branch_env(monkeypatch, ref_ok=False)
    comments = [{"author": 9, "body": "/ticket"}]
    client = FakeClient(comments)
    ticket = {"id": 5, "tags": [rt.TAG_AWAIT_PR], "status": "in_review",
              "created_by": 9, "title": "t", "priority": "low"}

    rt.process(fake_cfg, client, ticket, dry_run=False)

    assert client.created == []
    assert any("no published branch" in body for _, body in client.comments_added)


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
