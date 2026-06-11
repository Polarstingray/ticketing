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

    def __init__(self, comments=None):
        self._comments = comments or []
        self.comments_added: list[tuple[int, str]] = []
        self.updates: list[dict] = []

    def list_comments(self, ticket_id):
        return self._comments

    def add_comment(self, ticket_id, body):
        self.comments_added.append((ticket_id, body))
        return {"id": len(self.comments_added)}

    def update_ticket(self, ticket_id, **fields):
        self.updates.append(fields)
        return {"id": ticket_id, **fields}


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
        # free-resolver knobs (off by default; individual tests opt in)
        escalate_to_user_id=0,
        escalate_priorities=["high", "critical"],
        review_api_url="",
        review_api_key="",
        review_api_model="",
        # verification gate (off by default; individual tests opt in)
        verify_command="",
        verify_timeout=900,
        verify_max_retries=1,
    )


@pytest.fixture(autouse=True)
def _quiet_logger():
    """Keep the 'resolver' logger from spamming stderr during tests."""
    logger = logging.getLogger("resolver")
    prev = list(logger.handlers)
    logger.handlers = [logging.NullHandler()]
    yield
    logger.handlers = prev
