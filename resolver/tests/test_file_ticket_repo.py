"""A resolver-filed ticket names the repo the run is working ON, not the directory
the agent happens to be running IN.

Ticket #41 was correctly tagged `repo:ticketing`. The resolver then filed #42 as
`repo:resolver-ticketing` (its own checkout) and #43 as `repo:ticket-42` (its
worktree), because file_ticket fell straight through to cwd-based derivation.
"""
import argparse
import subprocess

import pytest

import file_ticket


def _git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True,
                          check=True).stdout


@pytest.fixture
def worktree(tmp_path):
    """A checkout named `ticketing` plus a worktree named `ticket-42`, mirroring the
    real layout the agent files from."""
    repo = tmp_path / "ticketing"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text("hi\n")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    wt = tmp_path / "work" / "ticket-42"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt))
    yield repo, wt
    _git(repo, "worktree", "remove", "--force", str(wt))


def _args(root, **kw):
    return argparse.Namespace(type="task", title="t", description="", priority="medium",
                              tag=None, code_block=None, root=str(root), assign=None,
                              **kw)


def _repo_tags(payload):
    return [t for t in payload["tags"] if t.startswith("repo:")]


def test_inherits_the_repo_the_resolver_is_working_on(worktree, monkeypatch):
    _repo, wt = worktree
    monkeypatch.setenv("STINGRAY_TICKET_REPO", "ticketing")
    payload = file_ticket.build_payload(_args(wt))
    assert _repo_tags(payload) == ["repo:ticketing"]


def test_the_run_beats_an_agent_supplied_repo(worktree, monkeypatch):
    """Inside a sweep the run is authoritative — an agent's --repo does NOT win.

    This is #46: the agent was told to `cd` into the resolver's own checkout to run
    the filer and passed `--repo resolver-ticketing`, so the ticket it filed pointed
    at the resolver's clone. The whole implement run then happened there and could
    not be pushed. The run knew it was working on `ticketing`; that has to win.
    """
    _repo, wt = worktree
    monkeypatch.setenv("STINGRAY_TICKET_REPO", "ticketing")
    payload = file_ticket.build_payload(_args(wt, repo="resolver-ticketing"))
    assert _repo_tags(payload) == ["repo:ticketing"]


def test_explicit_repo_wins_outside_a_sweep(worktree, monkeypatch):
    """A human at a shell has no STINGRAY_TICKET_REPO, so --repo is authoritative."""
    _repo, wt = worktree
    monkeypatch.delenv("STINGRAY_TICKET_REPO", raising=False)
    payload = file_ticket.build_payload(_args(wt, repo="other"))
    assert _repo_tags(payload) == ["repo:other"]


def test_no_repo_opts_out_even_with_an_explicit_repo(worktree, monkeypatch):
    """--no-repo is the opt-out for both inputs; neither can resurrect a tag."""
    _repo, wt = worktree
    monkeypatch.setenv("STINGRAY_TICKET_REPO", "ticketing")
    payload = file_ticket.build_payload(_args(wt, repo="other", no_repo=True))
    assert _repo_tags(payload) == []


def test_falls_back_to_derivation_when_unset(worktree, monkeypatch):
    """A CLI run outside a resolver sweep keeps deriving — and now resolves the
    worktree to its repo rather than naming it `ticket-42`."""
    _repo, wt = worktree
    monkeypatch.delenv("STINGRAY_TICKET_REPO", raising=False)
    payload = file_ticket.build_payload(_args(wt))
    assert _repo_tags(payload) == ["repo:ticketing"]


def test_blank_env_does_not_win_over_derivation(worktree, monkeypatch):
    # process() exports "" when a ticket carries no repo tag and DEFAULT_REPO is unset.
    _repo, wt = worktree
    monkeypatch.setenv("STINGRAY_TICKET_REPO", "   ")
    payload = file_ticket.build_payload(_args(wt))
    assert _repo_tags(payload) == ["repo:ticketing"]


def test_no_repo_still_opts_out(worktree, monkeypatch):
    _repo, wt = worktree
    monkeypatch.setenv("STINGRAY_TICKET_REPO", "ticketing")
    payload = file_ticket.build_payload(_args(wt, no_repo=True))
    assert _repo_tags(payload) == []
