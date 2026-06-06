"""Tests for ticket #15 input-handling fixes.

1. The tag filter escapes LIKE metacharacters ('%' / '_') so attacker-supplied
   wildcards are matched literally instead of over-matching the filter.
2. Email Subject headers are sanitized of CR/LF so a crafted ticket title can't
   inject additional headers (Bcc, etc.).
"""
import os
import tempfile

import pytest

# Use an isolated SQLite file before importing app modules.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
from database import SessionLocal  # noqa: E402
from main import app  # noqa: E402
from models import ApiKey, Ticket, User  # noqa: E402
from notifications import _sanitize_header  # noqa: E402
from seed import seed_admin  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _setup():
    from database import Base, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_admin(db)
        admin = db.query(User).filter(User.username == "admin").first()
        # Seed tickets whose tags differ only at the position a wildcard would span.
        db.add(Ticket(type="task", title="abc ticket", created_by=admin.id, tags=["abc"]))
        db.add(Ticket(type="task", title="axc ticket", created_by=admin.id, tags=["axc"]))
        db.commit()
    finally:
        db.close()
    yield


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


def _tags(items):
    return sorted(t for item in items for t in item["tags"])


def test_underscore_is_literal_not_wildcard():
    key = _admin_key()
    c = TestClient(app)
    # '_' would match any single char ('abc' and 'axc') if unescaped; escaped it
    # is a literal underscore and matches neither.
    r = c.get("/tickets", params={"tag": "a_c"}, headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0


def test_exact_tag_still_matches():
    key = _admin_key()
    c = TestClient(app)
    r = c.get("/tickets", params={"tag": "abc"}, headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert _tags(body["items"]) == ["abc"]


def test_percent_is_literal_not_wildcard():
    key = _admin_key()
    c = TestClient(app)
    # '%' would match every ticket if unescaped; escaped it matches no real tag.
    r = c.get("/tickets", params={"tag": "%"}, headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0


def test_sanitize_header_strips_crlf():
    cleaned = _sanitize_header("evil\r\nBcc: x@y.com")
    assert "\r" not in cleaned and "\n" not in cleaned
    # Everything stays on a single line; no header can be injected.
    assert "\n" not in cleaned
    assert cleaned == "evilBcc: x@y.com"


def test_send_email_subject_has_no_newline(monkeypatch):
    import notifications

    captured = {}

    class FakeSMTP:
        """Stand-in for smtplib.SMTP that opens no socket and records the message."""
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def send_message(self, msg):
            captured["subject"] = msg["Subject"]

    # Enable email so send_email builds and "sends" a message, but capture the
    # outgoing EmailMessage instead of talking to a real SMTP server.
    monkeypatch.setattr(notifications, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)

    notifications.send_email(
        ["admin@example.com"],
        "New ticket #1: evil\r\nBcc: attacker@example.com",
        "body",
    )

    assert "subject" in captured
    assert "\r" not in captured["subject"]
    assert "\n" not in captured["subject"]
