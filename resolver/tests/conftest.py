"""Shared test fixtures. Puts the resolver package dir on sys.path so the
flat modules (resolve_tickets, stingray, config, audit) import cleanly."""
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import RepoNotFound

RESOLVER_DIR = Path(__file__).resolve().parent.parent
if str(RESOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(RESOLVER_DIR))

BOT = 2


class FakeClient:
    """Records the writes the resolver makes so tests can assert on them."""

    def __init__(self, comments=None, tickets=None):
        self._comments = comments or []
        self.comments_added: list[tuple[int, str]] = []
        self.updates: list[dict] = []
        self.created: list[dict] = []
        self._next_id = 101
        # id -> ticket dict, for get_ticket/iter_tickets (delegation paths).
        self._tickets: dict[int, dict] = {t["id"]: t for t in (tickets or [])}

    def list_comments(self, ticket_id):
        return self._comments

    def create_ticket(self, **fields):
        self.created.append(fields)
        ticket = {"id": self._next_id, **fields}
        self._next_id += 1
        self._tickets[ticket["id"]] = ticket
        return ticket

    def add_comment(self, ticket_id, body):
        self.comments_added.append((ticket_id, body))
        return {"id": len(self.comments_added)}

    def update_ticket(self, ticket_id, **fields):
        self.updates.append(fields)
        return {"id": ticket_id, **fields}

    def heartbeat(self, **fields):
        self.heartbeats = getattr(self, "heartbeats", [])
        self.heartbeats.append(fields)
        return {"bot_user_id": self.__dict__.get("bot_user_id", 0), **fields}

    def get_ticket(self, ticket_id):
        return self._tickets[ticket_id]

    def iter_tickets(self, **filters):
        tag = filters.get("tag")
        for t in self._tickets.values():
            if tag is None or tag in (t.get("tags") or []):
                yield t


@pytest.fixture
def fake_cfg(tmp_path):
    """A stand-in Config with just the attributes the tested paths read."""
    def resolve_repo(name):
        # Mirror the real contract: an empty/None name (no `repo:` tag and no
        # DEFAULT_REPO) raises RepoNotFound; a named repo resolves to a path.
        if not (name or "").strip():
            raise RepoNotFound("no repo specified (add a `repo:<name>` tag) and "
                               "DEFAULT_REPO is unset")
        return tmp_path / "repo"

    return SimpleNamespace(
        bot_user_id=BOT,
        agent="claude",
        max_attempts=3,
        patch_fallback=False,
        git_net_timeout=300,
        git_author_name="Test Bot",
        git_author_email="bot@test.local",
        audit_output_tail_bytes=4096,
        logs_dir=tmp_path,
        default_repo=None,
        resolve_repo=resolve_repo,
        # implement model + difficulty-routed tiers (blank = no swap)
        agent_implement_model="",
        agent_implement_model_easy="",
        agent_implement_model_hard="",
        # free-resolver knobs (off by default; individual tests opt in)
        escalate_to_user_id=0,
        escalate_priorities=["high", "critical"],
        review_api_url="",
        review_api_key="",
        review_api_model="",
        # plan-critique gate (off by default; individual tests opt in)
        critique_api_url="",
        critique_api_key="",
        critique_api_model="",
        critique_max_revisions=1,
        # verification gate (off by default; individual tests opt in)
        verify_command="",
        verify_timeout=900,
        verify_max_retries=1,
        # quota backoff window (minutes) before a parked ticket auto-retries
        quota_backoff_minutes=60,
        # resolver-to-resolver delegation (off by default; tests opt in)
        allow_delegation=False,
        workers=[],
        max_delegations=10,
        # Connection details for the ticket-lease client `sweep` builds per
        # ticket. Tests stub the client itself; these just have to exist.
        stingray_url="http://stingray.test",
        api_key="test-key",
        stingray_max_retries=1,
    )


@pytest.fixture(autouse=True)
def _no_inherited_ticket_repo(monkeypatch):
    """`process()` exports STINGRAY_TICKET_REPO so tickets the agent files name the
    repo the run is working on. It is a real process env var, so without this a test
    that drives process() leaks it into every later test that derives a repo tag."""
    monkeypatch.delenv("STINGRAY_TICKET_REPO", raising=False)


@pytest.fixture(autouse=True)
def _stub_readonly_worktrees(monkeypatch, tmp_path):
    """do_plan and do_review each build a detached worktree at the ticket's pinned
    commit, so they read the code the ticket was filed against rather than whatever
    the checkout is sitting on. These unit tests point at a bare tmp_path, not a real
    git repo, so stand the helper down to a plain directory. Tests that care about the
    real git behavior (the resolve_base / pinning tests) call it directly and are
    unaffected — this only replaces the module-level helper the phase handlers use.
    """
    import resolve_tickets as rt

    def _fake(repo, ticket_id, base_ref, kind):
        wt = tmp_path / f"{kind}-wt-{ticket_id}"
        wt.mkdir(parents=True, exist_ok=True)
        return wt

    monkeypatch.setattr(rt, "prepare_readonly_worktree", _fake)
    monkeypatch.setattr(rt, "remove_worktree", lambda repo, wt: None)


@pytest.fixture(autouse=True)
def _quiet_logger():
    """Keep the 'resolver' logger from spamming stderr during tests."""
    logger = logging.getLogger("resolver")
    prev = list(logger.handlers)
    logger.handlers = [logging.NullHandler()]
    yield
    logger.handlers = prev
