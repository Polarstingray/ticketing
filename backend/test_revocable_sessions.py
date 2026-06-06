"""Tests for revocable sessions (ticket #14).

Verifies that logout, password change, and role change invalidate existing
session cookies, and that pre-change (versionless) tokens are rejected.
"""
import os
import tempfile

import pytest

# Use an isolated SQLite file per test session before importing app modules.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
from database import SessionLocal  # noqa: E402
from main import app  # noqa: E402
from models import ApiKey, User  # noqa: E402
from seed import seed_admin  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _setup():
    from database import Base, engine
    from ratelimit import limiter

    # Disable the per-IP login rate limit for the suite: it shares one client IP
    # and logs in many times, which would otherwise trip the 5/minute limit.
    # Setting the boolean flag is honored by slowapi's `if self.enabled` guards.
    limiter.enabled = False

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_admin(db)
    finally:
        db.close()
    yield


def _client():
    return TestClient(app)


def _login(client, username="admin", password="admin"):
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


def _admin_key():
    """Mint a fresh API key for the seeded admin and return its raw value.

    The seeded key's plaintext isn't recoverable (only its hash is stored), so
    tests authenticating as admin via X-API-Key mint their own against the
    ApiKey table.
    """
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        raw = auth.generate_api_key()
        db.add(ApiKey(
            user_id=admin.id,
            name="test",
            key_prefix=raw[:11],
            key_hash=auth.hash_api_key(raw),
        ))
        db.commit()
        return raw
    finally:
        db.close()


def test_login_me_works():
    c = _client()
    _login(c)
    assert c.get("/auth/me").status_code == 200


def test_logout_invalidates_reused_cookie():
    c = _client()
    _login(c)
    cookie = c.cookies.get("session")
    assert c.get("/auth/me").status_code == 200

    c.post("/auth/logout")

    # Reuse the *same* old cookie value on a fresh client -> must be rejected.
    fresh = _client()
    fresh.cookies.set("session", cookie)
    assert fresh.get("/auth/me").status_code == 401


def test_unauthenticated_logout_still_ok():
    c = _client()
    assert c.post("/auth/logout").status_code == 200


def test_old_format_token_without_sv_rejected():
    # Mint a token in the pre-change format (no "sv").
    legacy = auth._serializer.dumps({"user_id": 1})
    c = _client()
    c.cookies.set("session", legacy)
    assert c.get("/auth/me").status_code == 401


def test_password_change_logs_out_all_devices():
    # Create a member user via the admin.
    key = _admin_key()
    admin = _client()
    r = admin.post(
        "/users",
        headers={"X-API-Key": key},
        json={
            "username": "bob",
            "display_name": "Bob",
            "email": "bob@example.com",
            "password": "pw1pw1",
            "role": "member",
        },
    )
    assert r.status_code == 201, r.text
    bob_id = r.json()["id"]

    # Bob logs in on two "devices".
    dev_a = _client()
    _login(dev_a, "bob", "pw1pw1")
    dev_b = _client()
    _login(dev_b, "bob", "pw1pw1")
    assert dev_a.get("/auth/me").status_code == 200
    assert dev_b.get("/auth/me").status_code == 200

    # Admin resets bob's password.
    admin.patch(f"/users/{bob_id}", headers={"X-API-Key": key}, json={"password": "pw2pw2"})

    assert dev_a.get("/auth/me").status_code == 401
    assert dev_b.get("/auth/me").status_code == 401

    # Fresh login with the new password works.
    dev_c = _client()
    _login(dev_c, "bob", "pw2pw2")
    assert dev_c.get("/auth/me").status_code == 200


def test_role_change_invalidates_session():
    key = _admin_key()
    admin = _client()
    r = admin.post(
        "/users",
        headers={"X-API-Key": key},
        json={
            "username": "carol",
            "display_name": "Carol",
            "email": "carol@example.com",
            "password": "pw1pw1",
            "role": "member",
        },
    )
    assert r.status_code == 201, r.text
    carol_id = r.json()["id"]

    dev = _client()
    _login(dev, "carol", "pw1pw1")
    assert dev.get("/auth/me").status_code == 200

    admin.patch(f"/users/{carol_id}", headers={"X-API-Key": key}, json={"role": "admin"})

    assert dev.get("/auth/me").status_code == 401


def test_api_key_auth_unaffected():
    key = _admin_key()
    c = _client()
    assert c.get("/auth/me", headers={"X-API-Key": key}).status_code == 200
