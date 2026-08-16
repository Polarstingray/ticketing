"""Shared fixtures. Real temp git repos, and a FakeClient that records writes."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


class FakeClient:
    """Records the API calls a command makes so tests can assert on them."""

    def __init__(self, tickets=None):
        self.created: list[dict] = []
        self.updated: list[tuple[int, dict]] = []
        self._tickets = tickets or {}
        self._next_id = 100

    def create_ticket(self, **fields):
        self.created.append(fields)
        ticket = {"id": self._next_id, **fields}
        self._next_id += 1
        return ticket

    def update_ticket(self, ticket_id, **fields):
        self.updated.append((ticket_id, fields))
        return {"id": ticket_id, **fields}

    def get_ticket(self, ticket_id):
        return self._tickets.get(ticket_id, {})

    def whoami(self):
        return {"id": 5, "username": "tester"}


@pytest.fixture
def fake_client():
    return FakeClient()


def run_git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """An initialized repo with an identity configured, no commits yet."""
    root = tmp_path / "repo"
    root.mkdir()
    run_git(root, "init", "-q")
    run_git(root, "config", "user.email", "test@example.com")
    run_git(root, "config", "user.name", "Test")
    run_git(root, "checkout", "-q", "-b", "main")
    return root


@pytest.fixture
def commit(git_repo):
    """Write files and commit them; returns the new SHA."""
    def _commit(message: str, files: dict[str, str]) -> str:
        for name, content in files.items():
            target = git_repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        run_git(git_repo, "add", "-A")
        run_git(git_repo, "commit", "-q", "-m", message)
        return run_git(git_repo, "rev-parse", "HEAD").strip()
    return _commit


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point the credential store at a throwaway file."""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("STINGRAY_CONFIG", str(path))
    monkeypatch.delenv("STINGRAY_URL", raising=False)
    monkeypatch.delenv("STINGRAY_API_KEY", raising=False)
    monkeypatch.delenv("STINGRAY_PROFILE", raising=False)
    return path
