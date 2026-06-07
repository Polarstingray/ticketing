"""Tests for security headers and the narrowed CORS policy (ticket #13)."""
import os
import tempfile

import pytest

# Use an isolated SQLite file before importing app modules.
_tmp = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import main  # noqa: E402
from database import SessionLocal  # noqa: E402
from main import app  # noqa: E402
from models import ApiKey, User  # noqa: E402
from seed import seed_admin  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _setup():
    from database import Base, engine
    from ratelimit import limiter

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


def _admin_key():
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


def test_health_has_security_headers():
    r = _client().get("/health")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_authed_endpoint_has_security_headers():
    key = _admin_key()
    r = _client().get("/auth/me", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in r.headers


def test_hsts_absent_in_dev():
    # COOKIE_SECURE is false in dev -> no HSTS (would poison local HTTP).
    assert main.COOKIE_SECURE is False
    r = _client().get("/health")
    assert "Strict-Transport-Security" not in r.headers


def test_hsts_present_in_production(monkeypatch):
    # Simulate HTTPS/prod: the middleware reads main.COOKIE_SECURE at request time.
    monkeypatch.setattr(main, "COOKIE_SECURE", True)
    r = _client().get("/health")
    assert "Strict-Transport-Security" in r.headers
    assert "max-age=63072000" in r.headers["Strict-Transport-Security"]


def test_cors_preflight_narrowed_for_allowed_origin():
    r = _client().options(
        "/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200
    allow_methods = r.headers.get("Access-Control-Allow-Methods", "")
    assert "GET" in allow_methods
    # The wildcard is gone — methods are explicitly enumerated.
    assert "*" not in allow_methods
    assert r.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"


def test_cors_disallowed_origin_not_reflected():
    r = _client().options(
        "/health",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.headers.get("Access-Control-Allow-Origin") != "http://evil.example.com"
