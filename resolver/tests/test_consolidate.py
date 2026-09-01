"""Unit tests for the `/consolidate` directive (resolve_tickets.py).

No network, no real git/gh — `run()` and `has_origin()` are monkeypatched with a
fake dispatcher that mimics `gh`/`git` well enough to exercise merge/conflict/push/
PR-create branching and the follow-up code-review filing.
"""
import json

import pytest

import resolve_tickets as rt
from conftest import BOT, FakeClient


# --- directive parsing -----------------------------------------------------
def test_parse_consolidate_prs_bare():
    assert rt._parse_consolidate_prs("/consolidate") == []


def test_parse_consolidate_prs_with_numbers():
    assert rt._parse_consolidate_prs("/consolidate 12 15") == [12, 15]


def test_parse_consolidate_prs_dedupes_preserving_order():
    assert rt._parse_consolidate_prs("/consolidate 12 12 15") == [12, 15]


def test_parse_consolidate_prs_malformed_raises():
    with pytest.raises(rt._DirectiveError):
        rt._parse_consolidate_prs("/consolidate not-a-number")


def test_collect_consolidate_directives_from_body_and_comments():
    ticket = {"description": "/consolidate 1 2", "created_by": 9}
    comments = [
        {"author": 9, "body": "/consolidate 3"},
        {"author": BOT, "body": "/consolidate 4"},  # bot-authored: ignored
    ]
    found = rt.collect_consolidate_directives(ticket, comments, BOT)
    assert [d["line"] for d in found] == ["/consolidate 1 2", "/consolidate 3"]


def test_collect_consolidate_directives_ignores_unrelated_lines():
    ticket = {"description": "please /consolidate-ish this\nnormal text", "created_by": 9}
    assert rt.collect_consolidate_directives(ticket, [], BOT) == []


# --- do_consolidate ---------------------------------------------------------
class _FakeRun:
    """Dispatches fake `git`/`gh` argv to canned results, recording every call."""

    def __init__(self, *, conflicting_prs=(), pr_list=None, pr_create_ok=True,
                 abort_fails=False):
        self.calls: list[list[str]] = []
        self.conflicting_prs = set(conflicting_prs)
        self.pr_list = pr_list if pr_list is not None else [
            {"number": 10, "baseRefName": "main"},
            {"number": 11, "baseRefName": "main"},
        ]
        self.pr_create_ok = pr_create_ok
        self.abort_fails = abort_fails
        self.merge_in_progress = False

    def __call__(self, cmd, cwd=None, timeout=None):
        self.calls.append(list(cmd))
        if cmd[0] == "gh":
            if cmd[1:3] == ["auth", "status"]:
                return 0, ""
            if cmd[1:3] == ["repo", "view"]:
                return 0, "main\n"
            if cmd[1:3] == ["pr", "list"]:
                return 0, json.dumps(self.pr_list)
            if cmd[1:3] == ["pr", "create"]:
                if self.pr_create_ok:
                    return 0, "https://github.com/acme/repo/pull/99\n"
                return 1, "a pull request for branch already exists"
            if cmd[1:3] == ["pr", "view"]:
                return 0, "https://github.com/acme/repo/pull/99"
            return 0, ""
        if cmd[0] == "git":
            if "worktree" in cmd:
                if "add" in cmd:
                    wt = cmd[-2]
                    from pathlib import Path
                    Path(wt).mkdir(parents=True, exist_ok=True)
                return 0, ""
            if "fetch" in cmd:
                pull_ref = next((a for a in cmd if a.startswith("pull/")), None)
                if pull_ref:
                    n = int(pull_ref.split("/")[1])
                    if n in self.conflicting_prs:
                        return 0, ""  # the fetch itself succeeds; merge below conflicts
                return 0, ""
            if "merge" in cmd:
                if "--abort" in cmd:
                    if self.abort_fails:
                        return 1, "error: There is no merge to abort"
                    if self.merge_in_progress:
                        self.merge_in_progress = False
                    return 0, ""
                pr_ref = next((a for a in cmd if a.startswith("pr-")), None)
                if pr_ref and int(pr_ref[len("pr-"):]) in self.conflicting_prs:
                    self.merge_in_progress = True
                    return 1, "CONFLICT (content): merge conflict in x.py"
                return 0, ""
            if "push" in cmd:
                return 0, ""
            if cmd[-2:] == ["rev-parse", "HEAD"] or "rev-parse" in cmd:
                return 0, "deadbeefcafe\n"
            return 0, ""
        return 0, ""


@pytest.fixture
def consolidate_ticket(fake_cfg):
    fake_cfg.consolidate_review_user_id = 4
    ticket = {"id": 50, "tags": ["repo:x"], "status": "open", "created_by": 9,
              "description": "/consolidate"}
    return ticket


def test_do_consolidate_merges_and_skips_conflicts(fake_cfg, monkeypatch, consolidate_ticket, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_run = _FakeRun(conflicting_prs={11})
    monkeypatch.setattr(rt, "run", fake_run)
    monkeypatch.setattr(rt, "has_origin", lambda r: True)
    monkeypatch.setattr(rt, "remove_worktree", lambda r, wt: None)

    client = FakeClient([])
    directive = rt.collect_consolidate_directives(consolidate_ticket, [], BOT)[0]

    rt.do_consolidate(fake_cfg, client, consolidate_ticket, repo, directive)

    # Follow-up code-review ticket filed correctly.
    assert len(client.created) == 1
    review = client.created[0]
    assert review["type"] == "code_review"
    assert review["assigned_to"] == 4
    assert "repo:repo" in review["tags"]
    assert "branch:claude/consolidate-50" in review["tags"]
    assert any(t.startswith("rev:") for t in review["tags"])

    # Marker comment records what merged/skipped and carries the dedupe key.
    marker_bodies = [b for _, b in client.comments_added if rt.CONSOLIDATE_MARKER in b]
    assert len(marker_bodies) == 1
    assert "#10" in marker_bodies[0] and "#11" in marker_bodies[0]
    assert f"[key:{directive['key']}]" in marker_bodies[0]

    # Original ticket handed back to its creator.
    assert client.updates[-1]["assigned_to"] == 9
    assert client.updates[-1]["status"] == "in_review"


def test_do_consolidate_explicit_pr_numbers_skips_pr_list(fake_cfg, monkeypatch, consolidate_ticket, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_run = _FakeRun()
    monkeypatch.setattr(rt, "run", fake_run)
    monkeypatch.setattr(rt, "has_origin", lambda r: True)
    monkeypatch.setattr(rt, "remove_worktree", lambda r, wt: None)
    consolidate_ticket["description"] = "/consolidate 10"
    client = FakeClient([])
    directive = rt.collect_consolidate_directives(consolidate_ticket, [], BOT)[0]

    rt.do_consolidate(fake_cfg, client, consolidate_ticket, repo, directive)

    assert not any(c[:3] == ["gh", "pr", "list"] for c in fake_run.calls)
    assert len(client.created) == 1


def test_do_consolidate_merge_abort_failure_bails_out(fake_cfg, monkeypatch, consolidate_ticket, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_run = _FakeRun(conflicting_prs={10}, abort_fails=True)
    monkeypatch.setattr(rt, "run", fake_run)
    monkeypatch.setattr(rt, "has_origin", lambda r: True)
    monkeypatch.setattr(rt, "remove_worktree", lambda r, wt: None)
    client = FakeClient([])
    directive = rt.collect_consolidate_directives(consolidate_ticket, [], BOT)[0]

    rt.do_consolidate(fake_cfg, client, consolidate_ticket, repo, directive)

    assert not client.created
    marker_bodies = [b for _, b in client.comments_added if rt.CONSOLIDATE_MARKER in b]
    assert len(marker_bodies) == 1
    assert "corrupted" in marker_bodies[0]
    assert client.updates[-1]["status"] == "in_review"


def test_do_consolidate_no_open_prs(fake_cfg, monkeypatch, consolidate_ticket, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_run = _FakeRun(pr_list=[])
    monkeypatch.setattr(rt, "run", fake_run)
    monkeypatch.setattr(rt, "has_origin", lambda r: True)
    client = FakeClient([])
    directive = rt.collect_consolidate_directives(consolidate_ticket, [], BOT)[0]

    rt.do_consolidate(fake_cfg, client, consolidate_ticket, repo, directive)

    assert not client.created
    assert any("No open PRs" in b for _, b in client.comments_added)
    assert client.updates[-1]["assigned_to"] == 9


def test_do_consolidate_all_conflict_opens_no_pr(fake_cfg, monkeypatch, consolidate_ticket, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_run = _FakeRun(conflicting_prs={10, 11})
    monkeypatch.setattr(rt, "run", fake_run)
    monkeypatch.setattr(rt, "has_origin", lambda r: True)
    monkeypatch.setattr(rt, "remove_worktree", lambda r, wt: None)
    client = FakeClient([])
    directive = rt.collect_consolidate_directives(consolidate_ticket, [], BOT)[0]

    rt.do_consolidate(fake_cfg, client, consolidate_ticket, repo, directive)

    assert not client.created
    assert not any(c[:3] == ["gh", "pr", "create"] for c in fake_run.calls)
    assert any("every PR conflicted" in b for _, b in client.comments_added)


def test_do_consolidate_no_repo(fake_cfg, monkeypatch, consolidate_ticket):
    client = FakeClient([])
    directive = rt.collect_consolidate_directives(consolidate_ticket, [], BOT)[0]

    rt.do_consolidate(fake_cfg, client, consolidate_ticket, None, directive)

    assert not client.created
    assert any("no `repo:` tag" in b for _, b in client.comments_added)


def test_do_consolidate_no_origin(fake_cfg, monkeypatch, consolidate_ticket, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(rt, "has_origin", lambda r: False)
    client = FakeClient([])
    directive = rt.collect_consolidate_directives(consolidate_ticket, [], BOT)[0]

    rt.do_consolidate(fake_cfg, client, consolidate_ticket, repo, directive)

    assert not client.created
    assert any("no `origin` remote" in b for _, b in client.comments_added)


def test_do_consolidate_malformed_directive(fake_cfg, monkeypatch, consolidate_ticket, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(rt, "has_origin", lambda r: True)
    monkeypatch.setattr(rt, "run", lambda cmd, cwd=None, timeout=None: (0, ""))
    consolidate_ticket["description"] = "/consolidate not-a-number"
    client = FakeClient([])
    directive = rt.collect_consolidate_directives(consolidate_ticket, [], BOT)[0]

    rt.do_consolidate(fake_cfg, client, consolidate_ticket, repo, directive)

    assert not client.created
    assert any("Could not parse" in b for _, b in client.comments_added)


# --- dedupe / wiring ---------------------------------------------------------
def test_handle_consolidate_directives_dedupes_across_sweeps(fake_cfg, monkeypatch, tmp_path):
    ticket = {"id": 51, "tags": ["repo:x"], "status": "in_review", "created_by": 9,
              "description": "/consolidate"}
    key = rt.directive_key("/consolidate")
    comments = [{"author": BOT, "body": f"{rt.CONSOLIDATE_MARKER}\n\nnote\n\n[key:{key}]"}]

    called = []
    monkeypatch.setattr(rt, "do_consolidate", lambda *a, **k: called.append(a))

    handled = rt.handle_consolidate_directives(
        fake_cfg, FakeClient(comments), ticket, comments, tmp_path, dry_run=False)

    assert handled is False
    assert called == []


def test_handle_consolidate_directives_runs_new_directive(fake_cfg, monkeypatch, tmp_path):
    ticket = {"id": 52, "tags": ["repo:x"], "status": "open", "created_by": 9,
              "description": "/consolidate"}
    called = []
    monkeypatch.setattr(rt, "do_consolidate", lambda *a, **k: called.append(a))

    handled = rt.handle_consolidate_directives(
        fake_cfg, FakeClient([]), ticket, [], tmp_path, dry_run=False)

    assert handled is True
    assert len(called) == 1


def test_handle_consolidate_directives_dry_run_does_not_call(fake_cfg, monkeypatch, tmp_path):
    ticket = {"id": 53, "tags": ["repo:x"], "status": "open", "created_by": 9,
              "description": "/consolidate"}
    called = []
    monkeypatch.setattr(rt, "do_consolidate", lambda *a, **k: called.append(a))

    handled = rt.handle_consolidate_directives(
        fake_cfg, FakeClient([]), ticket, [], tmp_path, dry_run=True)

    assert handled is True
    assert called == []


def test_process_skips_normal_dispatch_when_consolidate_handled(fake_cfg, monkeypatch, tmp_path):
    """process() should not fall into plan/implement/review once /consolidate ran."""
    ticket = {"id": 54, "type": "task", "tags": ["repo:x"], "status": "open",
              "created_by": 9, "description": "/consolidate"}
    monkeypatch.setattr(rt, "do_plan", lambda *a, **k: pytest.fail("should not plan"))
    monkeypatch.setattr(rt, "do_consolidate", lambda *a, **k: None)

    client = FakeClient([])
    rt.process(fake_cfg, client, ticket, dry_run=False)
    # do_consolidate was stubbed to a no-op (it owns state itself normally); the
    # important assertion is that do_plan was never reached.


def test_body_is_directive_only_recognizes_consolidate():
    assert rt.body_is_directive_only({"description": "/consolidate 1 2"})
    assert rt.body_is_directive_only({"description": "/consolidate\n/ticket task x"})
    assert not rt.body_is_directive_only({"description": "/consolidate\nplease also fix bug"})
