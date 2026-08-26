"""Shared pytest fixtures for the backend test suite.

The whole suite runs against a single throwaway SQLite database. The `database`
module reads ``DATABASE_PATH`` exactly once at import, so it must be set here —
in conftest, which pytest imports before any test module — before any app module
is imported. Every test therefore shares one seeded admin; tests are written to
tolerate accumulated rows (assert by id/membership, not absolute counts).
"""
import os
import tempfile
import uuid
from types import SimpleNamespace

# Point the app at an isolated database BEFORE importing anything that reads it.
_tmpdir = tempfile.mkdtemp(prefix="stingray-tests-")
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")
os.environ.setdefault("APP_ENV", "dev")
# Keep the webhook dispatcher out of the suite. The `client` fixture runs the
# app's lifespan, which would otherwise start a task polling this database every
# second and making real outbound requests for any webhook a test creates.
# test_dispatcher.py drives `drain_once` directly instead, which is
# deterministic where a background task would be a race.
os.environ["DISPATCHER_ENABLED"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from auth import generate_api_key, hash_api_key, hash_password  # noqa: E402
from database import SessionLocal  # noqa: E402
from login_throttle import account_lockout, api_key_throttle  # noqa: E402
from main import app  # noqa: E402
from models import ApiKey, User, UserRole  # noqa: E402
from ratelimit import limiter  # noqa: E402


def _mint_key(user_id: int, name: str = "test", scopes: str = "") -> str:
    """Create an API key row for ``user_id`` and return its plaintext value.

    Only the hash is stored (there is no ``User.api_key`` column), so tests that
    authenticate via ``X-API-Key`` mint their own key against the ApiKey table.
    ``scopes`` is the comma-separated column value, e.g. ``"cli"``.
    """
    db = SessionLocal()
    try:
        raw = generate_api_key()
        db.add(ApiKey(
            user_id=user_id,
            name=name,
            key_prefix=raw[:11],
            key_hash=hash_api_key(raw),
            scopes=scopes,
        ))
        db.commit()
        return raw
    finally:
        db.close()


@pytest.fixture(scope="session", autouse=True)
def _disable_rate_limit():
    """Disable the slowapi per-IP limiter for the whole suite.

    All tests share one client IP and log in many times, which would otherwise
    trip the 5/minute login limit. Setting ``enabled = False`` is honored by
    slowapi's ``if self.enabled`` guards.
    """
    limiter.enabled = False
    yield


@pytest.fixture(autouse=True)
def _reset_throttles():
    """Clear the in-memory auth throttles between tests so per-account lockouts
    and per-IP API-key blocks from one test can't bleed into the next."""
    account_lockout._states.clear()
    api_key_throttle._states.clear()
    yield


@pytest.fixture(scope="session")
def client():
    """A TestClient whose lifespan creates tables, runs migrations, seeds admin."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def new_client(client):
    """Factory for fresh TestClient instances with independent cookie jars.

    Used by session/auth tests that simulate multiple browsers/devices. The app
    is already started (tables created, admin seeded) by the `client` fixture, so
    these plain clients don't re-run the lifespan.
    """
    return lambda: TestClient(app)


@pytest.fixture(scope="session")
def admin_id(client):
    db = SessionLocal()
    try:
        return db.query(User).filter(User.username == "admin").first().id
    finally:
        db.close()


@pytest.fixture(scope="session")
def admin_key(client):
    """A raw API key authenticating as the seeded admin."""
    return _mint_key(_admin_id())


def _admin_id() -> int:
    db = SessionLocal()
    try:
        return db.query(User).filter(User.username == "admin").first().id
    finally:
        db.close()


@pytest.fixture
def scoped_key():
    """Mint an extra API key for an existing user, carrying ``scopes``.

    Lets a test authenticate as the *same* user with and without a capability,
    which is how the scope-rides-the-key boundary gets exercised.
    """
    def _make(user_id: int, scopes: str = "cli") -> str:
        return _mint_key(user_id, name=f"scoped-{scopes or 'none'}", scopes=scopes)
    return _make


@pytest.fixture
def make_user(client):
    """Factory creating a fresh user (+ API key) with a unique username.

    Returns a SimpleNamespace with ``id``, ``username``, ``password`` and
    ``key`` (a raw API key). Defaults to a member; pass ``role="admin"`` for an
    admin.
    """
    def _make(role: str = UserRole.member.value, password: str = "member123",
              username: str | None = None) -> SimpleNamespace:
        username = username or f"user_{uuid.uuid4().hex[:8]}"
        db = SessionLocal()
        try:
            user = User(
                username=username,
                display_name=username,
                email=f"{username}@example.com",
                role=role,
                hashed_password=hash_password(password),
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            uid = user.id
        finally:
            db.close()
        return SimpleNamespace(
            id=uid, username=username, password=password, key=_mint_key(uid)
        )
    return _make
