"""Unit tests for structured prompt commands (commands.py) and their wiring
into the resolver's prompt builders and dispatch.

No network, no subprocesses — the agent runners are monkeypatched out.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import commands
import resolve_tickets as rt
from conftest import BOT, FakeClient


# --- loader --------------------------------------------------------------
def _write_cmd(tmp_path, monkeypatch, name, text):
    d = tmp_path / "commands"
    d.mkdir(exist_ok=True)
    (d / f"{name}.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(commands, "COMMANDS_DIR", d)
    return d


def test_load_valid_command(tmp_path, monkeypatch):
    _write_cmd(tmp_path, monkeypatch, "audit",
               "---\ntype: code_review\ndescription: An audit\npriority: high\n---\n"
               "Do the audit.\n")
    c = commands.load_command("audit")
    assert c is not None
    assert (c.name, c.type, c.description, c.priority) == (
        "audit", "code_review", "An audit", "high")
    assert c.body == "Do the audit."


def test_load_defaults_type_to_task(tmp_path, monkeypatch):
    _write_cmd(tmp_path, monkeypatch, "thing", "---\ndescription: x\n---\nBody.\n")
    c = commands.load_command("thing")
    assert c is not None and c.type == "task"


def test_load_missing_file_returns_none(tmp_path, monkeypatch):
    _write_cmd(tmp_path, monkeypatch, "exists", "Body.")
    assert commands.load_command("nope") is None


def test_load_bad_type_rejected(tmp_path, monkeypatch):
    _write_cmd(tmp_path, monkeypatch, "bad", "---\ntype: bogus\n---\nBody.")
    assert commands.load_command("bad") is None


def test_load_empty_body_rejected(tmp_path, monkeypatch):
    _write_cmd(tmp_path, monkeypatch, "empty", "---\ntype: task\n---\n")
    assert commands.load_command("empty") is None


def test_load_no_frontmatter_is_task_body(tmp_path, monkeypatch):
    _write_cmd(tmp_path, monkeypatch, "plain", "Just a body, no frontmatter.")
    c = commands.load_command("plain")
    assert c is not None and c.type == "task" and c.body == "Just a body, no frontmatter."


def test_reserved_names_never_load(tmp_path, monkeypatch):
    _write_cmd(tmp_path, monkeypatch, "review", "Body.")
    assert commands.load_command("review") is None


def test_available_commands_skips_readme_and_invalid(tmp_path, monkeypatch):
    d = _write_cmd(tmp_path, monkeypatch, "good", "---\ntype: task\n---\nB.")
    (d / "README.md").write_text("# docs", encoding="utf-8")
    (d / "broken.md").write_text("---\ntype: nope\n---\nB.", encoding="utf-8")
    assert commands.available_commands() == ["good"]


# --- detection -----------------------------------------------------------
def test_detect_from_body(tmp_path, monkeypatch):
    _write_cmd(tmp_path, monkeypatch, "audit", "---\ntype: code_review\n---\nAudit.")
    cmd, unknown = commands.detect_command(
        {"description": "/audit\n\nfocus on auth"}, [], BOT)
    assert cmd is not None and cmd.name == "audit" and unknown is None


def test_detect_from_human_comment(tmp_path, monkeypatch):
    _write_cmd(tmp_path, monkeypatch, "audit", "---\ntype: task\n---\nAudit.")
    cmd, _ = commands.detect_command(
        {"description": "no command here"},
        [{"author": 9, "body": "/audit"}], BOT)
    assert cmd is not None and cmd.name == "audit"


def test_detect_ignores_bot_authored(tmp_path, monkeypatch):
    _write_cmd(tmp_path, monkeypatch, "audit", "---\ntype: task\n---\nAudit.")
    cmd, unknown = commands.detect_command(
        {"description": "x"}, [{"author": BOT, "body": "/audit"}], BOT)
    assert cmd is None and unknown is None


@pytest.mark.parametrize("line", ["/ticket task x", "/approve", "/revise foo", "/review"])
def test_detect_skips_reserved_verbs(line):
    assert commands.detect_command({"description": line}, [], BOT) == (None, None)


def test_detect_unknown_returns_slug(tmp_path, monkeypatch):
    _write_cmd(tmp_path, monkeypatch, "audit", "---\ntype: task\n---\nAudit.")
    cmd, unknown = commands.detect_command({"description": "/bogus-cmd"}, [], BOT)
    assert cmd is None and unknown == "bogus-cmd"


# --- prompt injection (back-compat + content) ----------------------------
_T = {"id": 7, "title": "T", "priority": "low", "description": "d", "code_blocks": []}


def test_prompt_builders_byte_identical_without_command():
    repo = Path("/repo")
    assert rt.plan_prompt(_T, repo, None) == rt.plan_prompt(_T, repo, None, None)
    assert rt.review_prompt(_T, repo, False) == rt.review_prompt(_T, repo, False, None)
    assert rt.implement_prompt(_T, repo, "plan") == \
        rt.implement_prompt(_T, repo, "plan", command=None)


def test_prompt_builders_inject_command_body():
    repo = Path("/repo")
    cmd = commands.Command("audit", "code_review", "d", "high", "DO THE AUDIT")
    for prompt in (rt.plan_prompt(_T, repo, None, cmd),
                   rt.review_prompt(_T, repo, False, cmd),
                   rt.implement_prompt(_T, repo, "plan", command=cmd)):
        assert "DO THE AUDIT" in prompt          # premade prompt present
        assert 'standard "audit" command' in prompt
        assert "Title:\n```\nT\n```" in prompt    # ticket context still present (fenced)


def test_orchestrate_prompt_back_compat_and_injection():
    repo = Path("/repo")
    cfg = SimpleNamespace(max_delegations=5, workers=[])
    assert rt.orchestrate_prompt(_T, repo, cfg) == rt.orchestrate_prompt(_T, repo, cfg, None)
    cmd = commands.Command("security-audit", "code_review", "d", "high", "DO THE AUDIT")
    prompt = rt.orchestrate_prompt(_T, repo, cfg, cmd)
    assert "DO THE AUDIT" in prompt
    assert 'standard "security-audit" command' in prompt
    assert "DELEGATE each to another resolver" in prompt   # still the orchestrator


# --- routing -------------------------------------------------------------
def _patch_runners(monkeypatch):
    """Capture which runner the dispatch picks and the command threaded to it."""
    seen = {}
    monkeypatch.setattr(rt, "do_plan",
                        lambda *a, **k: seen.update(action="plan", command=a[-1]))
    monkeypatch.setattr(rt, "do_review",
                        lambda *a, **k: seen.update(action="review", command=a[-1]))
    monkeypatch.setattr(rt, "do_implement",
                        lambda *a, **k: seen.update(action="implement", command=a[-1]))
    monkeypatch.setattr(rt, "do_delegate",
                        lambda *a, **k: seen.update(action="delegate", command=a[-1]))
    return seen


def test_code_review_command_routes_a_task_ticket_to_review(
        tmp_path, monkeypatch, fake_cfg):
    _write_cmd(tmp_path, monkeypatch, "security-audit", "---\ntype: code_review\n---\nAUDIT")
    seen = _patch_runners(monkeypatch)
    client = FakeClient([])
    ticket = {"id": 7, "type": "task", "tags": ["repo:x"], "status": "open",
              "created_by": 9, "description": "/security-audit"}

    rt.process(fake_cfg, client, ticket, dry_run=False)

    assert seen["action"] == "review"
    assert seen["command"] is not None and seen["command"].name == "security-audit"


def test_task_command_routes_to_plan(tmp_path, monkeypatch, fake_cfg):
    _write_cmd(tmp_path, monkeypatch, "tidy", "---\ntype: task\n---\nTIDY")
    seen = _patch_runners(monkeypatch)
    client = FakeClient([])
    ticket = {"id": 8, "type": "task", "tags": ["repo:x"], "status": "open",
              "created_by": 9, "description": "/tidy"}

    rt.process(fake_cfg, client, ticket, dry_run=False)

    assert seen["action"] == "plan"
    assert seen["command"] is not None and seen["command"].name == "tidy"


def test_delegate_tag_threads_command_to_lead(tmp_path, monkeypatch, fake_cfg):
    # /security-audit + delegate: the delegate branch wins, but the command must
    # reach the lead so it audits per the premade prompt before fanning out fixes.
    _write_cmd(tmp_path, monkeypatch, "security-audit", "---\ntype: code_review\n---\nAUDIT")
    fake_cfg.allow_delegation = True
    fake_cfg.workers = [{"id": 3, "name": "open", "desc": ""}]
    seen = _patch_runners(monkeypatch)
    client = FakeClient([])
    ticket = {"id": 10, "type": "task", "tags": ["delegate", "repo:x"], "status": "open",
              "created_by": 9, "title": "audit", "description": "/security-audit"}

    rt.process(fake_cfg, client, ticket, dry_run=False)

    assert seen["action"] == "delegate"
    assert seen["command"] is not None and seen["command"].name == "security-audit"


def test_unknown_command_posts_one_time_notice_and_handles_normally(
        tmp_path, monkeypatch, fake_cfg):
    _write_cmd(tmp_path, monkeypatch, "real", "---\ntype: task\n---\nR")
    seen = _patch_runners(monkeypatch)
    client = FakeClient([])
    ticket = {"id": 9, "type": "task", "tags": ["repo:x"], "status": "open",
              "created_by": 9, "description": "/not-a-command"}

    rt.process(fake_cfg, client, ticket, dry_run=False)

    assert any(rt.UNKNOWN_CMD_MARKER in body for _, body in client.comments_added)
    # falls through to normal handling (no command threaded)
    assert seen["action"] == "plan" and seen["command"] is None


# --- the shipped library -------------------------------------------------
def test_shipped_codebase_review_command_loads():
    """The real file on disk, not a tmp_path fixture: a frontmatter typo here
    would silently make `/codebase-review` an unknown command."""
    c = commands.load_command("codebase-review")
    assert c is not None
    assert c.type == "task"
    assert c.description
    assert "file_ticket.py" in c.body and "--parent" in c.body
    assert "codebase-review" in commands.available_commands()
