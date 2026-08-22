"""End-to-end behavior of `stingray review` and `stingray file`."""
from __future__ import annotations

import json
import subprocess

import pytest

from stingray_cli import cmd_scaffold, common
from stingray_cli.config import save_profile
from stingray_cli.main import main

BODY = "\n".join(f"line {i}" for i in range(1, 41)) + "\n"


@pytest.fixture
def repo_with_change(git_repo, commit):
    commit("first", {"a.py": BODY})
    commit("make it better", {"a.py": BODY.replace("line 5\n", "CHANGED 5\n")})
    return git_repo


@pytest.fixture
def wired(isolated_config, monkeypatch, fake_client):
    """A stored profile plus a FakeClient standing in for the API."""
    save_profile("test", {"url": "http://stingray.test", "api_key": "sk_test",
                          "bot_user_id": 7})
    monkeypatch.setattr(common, "StingrayClient", lambda *a, **kw: fake_client)
    monkeypatch.setattr(cmd_scaffold, "client_from",
                        lambda args: (fake_client, common.profile_from(args)))
    return fake_client


def run(argv, cwd) -> int:
    return main([*argv, "-C", str(cwd)])


# --- review ------------------------------------------------------------------

def test_dry_run_does_not_post(repo_with_change, wired, capsys):
    assert run(["review", "--dry-run"], repo_with_change) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "code_review"
    assert payload["code_blocks"]
    assert wired.created == []


def test_repo_tag_is_derived(repo_with_change, wired, capsys):
    run(["review", "--dry-run"], repo_with_change)
    payload = json.loads(capsys.readouterr().out)
    assert payload["tags"][0] == f"repo:{repo_with_change.name}"


def test_no_repo_opts_out(repo_with_change, wired, capsys):
    run(["review", "--dry-run", "--no-repo"], repo_with_change)
    assert json.loads(capsys.readouterr().out)["tags"] == []


def test_explicit_repo_wins(repo_with_change, wired, capsys):
    run(["review", "--dry-run", "--repo", "other"], repo_with_change)
    assert json.loads(capsys.readouterr().out)["tags"][0] == "repo:other"


# --- commit pinning -----------------------------------------------------------
# The ticket records WHICH COMMIT it was filed against, so the resolver reviews that
# code rather than whatever the checkout is sitting on when the sweep runs.

def _tags(capsys):
    return json.loads(capsys.readouterr().out)["tags"]


def test_pins_the_commit_and_branch(repo_with_change, wired, capsys):
    head = subprocess.run(["git", "-C", str(repo_with_change), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    run(["review", "--dry-run"], repo_with_change)
    tags = _tags(capsys)
    assert f"rev:{head}" in tags
    assert len(head) == 40, "the pin must be a full sha, not an abbreviation"
    assert any(t.startswith("branch:") for t in tags)


def test_pin_follows_the_branch_you_filed_from(repo_with_change, wired, capsys):
    subprocess.run(["git", "-C", str(repo_with_change), "checkout", "-q", "-b", "feat/probe"],
                   check=True)
    run(["review", "--dry-run"], repo_with_change)
    assert "branch:feat/probe" in _tags(capsys)


def test_detached_head_pins_the_commit_only(repo_with_change, wired, capsys):
    head = subprocess.run(["git", "-C", str(repo_with_change), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(repo_with_change), "checkout", "-q", "--detach"], check=True)
    run(["review", "--dry-run"], repo_with_change)
    tags = _tags(capsys)
    # No branch for a fix to stack onto — the sha alone still pins the review.
    assert f"rev:{head}" in tags
    assert not any(t.startswith("branch:") for t in tags)


def test_overlong_branch_name_is_dropped_but_the_commit_still_pins(
        repo_with_change, wired, capsys):
    long_branch = "feat/" + "x" * 60
    subprocess.run(["git", "-C", str(repo_with_change), "checkout", "-q", "-b", long_branch],
                   check=True)
    run(["review", "--dry-run"], repo_with_change)
    out = capsys.readouterr()
    tags = json.loads(out.out)["tags"]
    assert any(t.startswith("rev:") for t in tags)
    assert not any(t.startswith("branch:") for t in tags)
    assert "too long to tag" in out.err


def test_no_repo_also_drops_the_pin(repo_with_change, wired, capsys):
    # Without a repo tag there is no checkout to resolve a sha against, so a pin
    # would point at nothing.
    run(["review", "--dry-run", "--no-repo"], repo_with_change)
    assert _tags(capsys) == []


def test_title_defaults_to_the_commit_subject(repo_with_change, wired, capsys):
    run(["review", "--dry-run", "--no-worktree"], repo_with_change)
    assert json.loads(capsys.readouterr().out)["title"] == "Review: make it better"


def test_explicit_title_wins(repo_with_change, wired, capsys):
    run(["review", "--dry-run", "-m", "My title"], repo_with_change)
    assert json.loads(capsys.readouterr().out)["title"] == "My title"


def test_reserved_tag_rejected_before_any_work(repo_with_change, wired, capsys):
    """Fails fast on an unsettable tag rather than after diffing, or on a 422."""
    assert run(["review", "--dry-run", "--tag", "dangerous"], repo_with_change) == 1
    err = capsys.readouterr().err
    assert "reserved tags cannot be set" in err
    assert wired.created == []


@pytest.mark.parametrize("tag", ["claude:planning", "parent:3", "review-by:4",
                                 "delegate", "fix"])
def test_every_control_tag_is_rejected(tag, repo_with_change, wired):
    assert run(["review", "--dry-run", "--tag", tag], repo_with_change) == 1


def test_repo_tag_is_allowed_through(repo_with_change, wired, capsys):
    """repo: is the one reserved prefix a cli-scoped key may set."""
    assert run(["review", "--dry-run", "--tag", "repo:mine", "--no-repo"],
               repo_with_change) == 0
    assert "repo:mine" in json.loads(capsys.readouterr().out)["tags"]


def test_free_tags_pass_through(repo_with_change, wired, capsys):
    run(["review", "--dry-run", "--no-repo", "--tag", "backend", "--tag", "epic:4"],
        repo_with_change)
    assert json.loads(capsys.readouterr().out)["tags"] == ["backend", "epic:4"]


def test_assign_bot_uses_the_profile(repo_with_change, wired, capsys):
    run(["review", "--dry-run", "--assign-bot"], repo_with_change)
    assert json.loads(capsys.readouterr().out)["assigned_to"] == 7


def test_assign_bot_without_a_stored_id_errors(repo_with_change, isolated_config,
                                               monkeypatch, fake_client, capsys):
    save_profile("test", {"url": "http://x", "api_key": "sk_t"})  # no bot_user_id
    monkeypatch.setattr(common, "StingrayClient", lambda *a, **kw: fake_client)
    assert run(["review", "--dry-run", "--assign-bot"], repo_with_change) == 1
    assert "--bot-user-id" in capsys.readouterr().err


def test_posts_and_prints_the_url(repo_with_change, wired, capsys):
    assert run(["review", "--yes", "--no-repo"], repo_with_change) == 0
    assert len(wired.created) == 1
    out = capsys.readouterr().out
    assert "created ticket #100" in out
    assert "http://stingray.test/tickets/100" in out


def test_no_changes_is_an_error(git_repo, commit, wired, capsys):
    commit("only", {"README": "x\n"})
    # Reviewing a range with nothing in it.
    assert run(["review", "HEAD..HEAD", "--dry-run"], git_repo) == 1
    assert "no reviewable changes" in capsys.readouterr().err


def test_describe_fills_in_the_prose(repo_with_change, wired, monkeypatch, capsys):
    from stingray_cli import describe as describe_mod
    monkeypatch.setattr(
        describe_mod, "run_agent",
        lambda *a, **kw: '{"title": "Bound the retry", "description": "D", '
                         '"priority": "high", "tags": ["backend", "dangerous"]}',
    )
    run(["review", "--dry-run", "--no-repo", "--describe"], repo_with_change)
    payload = json.loads(capsys.readouterr().out)
    assert payload["title"] == "Bound the retry"
    assert payload["priority"] == "high"
    assert payload["tags"] == ["backend"], "the reserved tag must be dropped"


def test_describe_failure_still_files(repo_with_change, wired, monkeypatch, capsys):
    from stingray_cli import describe as describe_mod
    from stingray_cli.agent import AgentError

    def boom(*_a, **_kw):
        raise AgentError("no agent")
    monkeypatch.setattr(describe_mod, "run_agent", boom)

    assert run(["review", "--dry-run", "--no-repo", "--describe"], repo_with_change) == 0
    captured = capsys.readouterr()
    assert "Review: make it better" in captured.out
    assert "--describe failed" in captured.err


def test_explicit_title_beats_the_agent(repo_with_change, wired, monkeypatch, capsys):
    from stingray_cli import describe as describe_mod
    monkeypatch.setattr(describe_mod, "run_agent",
                        lambda *a, **kw: '{"title": "agent", "description": "D"}')
    run(["review", "--dry-run", "--no-repo", "--describe", "-m", "mine"],
        repo_with_change)
    assert json.loads(capsys.readouterr().out)["title"] == "mine"


# --- file --------------------------------------------------------------------

def test_file_builds_a_task(repo_with_change, wired, capsys):
    assert run(["file", "--type", "task", "--title", "T", "--dry-run"],
               repo_with_change) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "task"
    assert payload["code_blocks"] == []


def test_file_rejects_reserved_tags(repo_with_change, wired):
    assert run(["file", "--type", "task", "--title", "T", "--dry-run",
                "--tag", "delegate"], repo_with_change) == 1


# --- scaffold ----------------------------------------------------------------

def test_scaffold_groups_by_epic_never_parent(tmp_path, wired, monkeypatch):
    """The regression guard: `parent:<id>` would make each child self-driving —
    the resolver auto-approves its plan and implements it. A hand-written backlog
    must use the free `epic:` tag instead."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    dest = tmp_path / "proj"

    rc = main(["scaffold", "python-cli", str(dest), "--name", "proj",
               "--no-ai", "--yes"])
    assert rc == 0

    epic, *children = wired.created
    assert epic["title"].startswith("proj: scaffold")
    assert children, "each stub should get a ticket"
    for child in children:
        assert f"epic:{100}" in child["tags"]
        assert not any(t.startswith("parent:") for t in child["tags"])
        assert f"repo:{'proj'}" in child["tags"]


def test_scaffold_commits_before_filing(tmp_path, wired, monkeypatch):
    """Code blocks carry line numbers, so the tree must be committed first."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    dest = tmp_path / "proj"

    seen: list[bool] = []
    original = wired.create_ticket

    def spy(**fields):
        seen.append((dest / ".git").is_dir())
        return original(**fields)
    wired.create_ticket = spy

    main(["scaffold", "python-cli", str(dest), "--name", "proj", "--no-ai", "--yes"])
    assert seen and all(seen), "every ticket must be filed after git init/commit"


def test_scaffold_refuses_a_nonempty_dest(tmp_path, wired):
    dest = tmp_path / "proj"
    dest.mkdir()
    (dest / "existing.txt").write_text("hi")
    with pytest.raises(SystemExit) as exc:
        raise SystemExit(main(["scaffold", "python-cli", str(dest), "--yes"]))
    assert exc.value.code == 1


def test_scaffold_dry_run_files_nothing(tmp_path, wired, monkeypatch, capsys):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    dest = tmp_path / "proj"

    assert main(["scaffold", "python-cli", str(dest), "--name", "proj",
                 "--no-ai", "--dry-run", "--yes"]) == 0
    assert wired.created == []
    assert "[dry-run] epic:" in capsys.readouterr().out


def test_list_templates(wired, capsys):
    assert main(["scaffold", "--list-templates"]) == 0
    assert "python-cli" in capsys.readouterr().out
