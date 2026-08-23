"""`repo:<name>` derivation — which repo a filed ticket points the resolver at.

The tag is how the resolver finds a checkout; a wrong one is not cosmetic, it makes
the ticket unpickup-able. Tickets #42/#43 were both mis-tagged this way.
"""
import subprocess

import pytest

from stingray_client.tickets import build_payload, derive_repo_tag, main_checkout


def _git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True,
                          check=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "ticketing"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "f.txt").write_text("hi\n")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return r


def test_derives_the_checkout_name(repo):
    assert derive_repo_tag(repo) == "repo:ticketing"


def test_derives_from_a_subdirectory(repo):
    sub = repo / "cli" / "stingray_cli"
    sub.mkdir(parents=True)
    assert derive_repo_tag(sub) == "repo:ticketing"


def test_a_worktree_names_the_repo_it_belongs_to(repo, tmp_path):
    """The #43 bug: an agent files from inside the resolver's worktree, and the
    worktree's basename (`ticket-42`) resolves to nothing under PROJECTS_ROOT."""
    wt = tmp_path / "work" / "ticket-42"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt))
    try:
        assert derive_repo_tag(wt) == "repo:ticketing"
        assert derive_repo_tag(wt) != "repo:ticket-42"
        assert main_checkout(wt) == repo.resolve()
    finally:
        _git(repo, "worktree", "remove", "--force", str(wt))


def test_a_worktree_subdirectory_too(repo, tmp_path):
    wt = tmp_path / "work" / "ticket-42"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt))
    (wt / "cli").mkdir(exist_ok=True)
    try:
        assert derive_repo_tag(wt / "cli") == "repo:ticketing"
    finally:
        _git(repo, "worktree", "remove", "--force", str(wt))


def test_outside_a_repo_derives_nothing(tmp_path):
    assert derive_repo_tag(tmp_path) is None
    assert main_checkout(tmp_path) is None


def test_explicit_repo_still_wins_over_derivation(repo):
    payload = build_payload(type="task", title="x", root=repo, repo="other",
                            warn=lambda m: None)
    assert payload["tags"] == ["repo:other"]
