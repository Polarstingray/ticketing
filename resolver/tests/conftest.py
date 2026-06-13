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
        # id -> ticket dict, for get_ticket/iter_tickets (delegation paths).
        self._tickets: dict[int, dict] = {t["id"]: t for t in (tickets or [])}

    def list_comments(self, ticket_id):
        return self._comments

    def add_comment(self, ticket_id, body):
        self.comments_added.append((ticket_id, body))
        return {"id": len(self.comments_added)}

    def update_ticket(self, ticket_id, **fields):
        self.updates.append(fields)
        return {"id": ticket_id, **fields}

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
    )


@pytest.fixture(autouse=True)
def _quiet_logger():
    """Keep the 'resolver' logger from spamming stderr during tests."""
    logger = logging.getLogger("resolver")
    prev = list(logger.handlers)
    logger.handlers = [logging.NullHandler()]
    yield
    logger.handlers = prev
