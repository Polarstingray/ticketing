"""Tests for ticket archive / unarchive.

Each test module run uses an isolated temp SQLite database, selected by setting
DATABASE_PATH *before* importing the app (the database module reads it at import
time). Authentication uses the seeded admin's API key plus an extra member.
"""
import os
import tempfile

# Point the app at a throwaway database before anything imports `database`.
_tmpdir = tempfile.mkdtemp(prefix="stingray-archive-test-")
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ.setdefault("ADMIN_USERNAME", "admin")
# Keep this in sync with test_revocable_sessions.py: when both suites run in one
# pytest process they share a single seeded admin (the database module reads
# DATABASE_PATH once at import), so a mismatched admin password would 401 the
# other suite's login.
os.environ.setdefault("ADMIN_PASSWORD", "admin")

import pytest
from fastapi.testclient import TestClient

from database import SessionLocal
from main import app
from models import ApiKey, User, UserRole
from auth import generate_api_key, hash_api_key, hash_password


def _mint_key(db, user) -> str:
    """Create an API key for `user` via the ApiKey table and return its raw value
    (the integrated schema stores only the hash, not a User.api_key column)."""
    raw = generate_api_key()
    db.add(ApiKey(
        user_id=user.id,
        name="test",
        key_prefix=raw[:11],
        key_hash=hash_api_key(raw),
    ))
    db.commit()
    return raw


@pytest.fixture(scope="module")
def client():
    # The lifespan handler creates tables, runs the archived migration, and seeds
    # the admin user.
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_key():
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        return _mint_key(db, admin)
    finally:
        db.close()


@pytest.fixture(scope="module")
def member_key():
    """A second, non-admin user who is neither creator nor assignee of test tickets."""
    db = SessionLocal()
    try:
        member = db.query(User).filter(User.username == "member").first()
        if member is None:
            member = User(
                username="member",
                display_name="Member",
                email="member@example.com",
                role=UserRole.member.value,
                hashed_password=hash_password("member123"),
            )
            db.add(member)
            db.commit()
            db.refresh(member)
        return _mint_key(db, member)
    finally:
        db.close()


def _create_ticket(client, key, **overrides):
    body = {"type": "task", "title": "Archivable ticket"}
    body.update(overrides)
    r = client.post("/tickets", json=body, headers={"X-API-Key": key})
    assert r.status_code == 201, r.text
    return r.json()


def _set_status(client, key, ticket_id, status):
    r = client.patch(
        f"/tickets/{ticket_id}", json={"status": status}, headers={"X-API-Key": key}
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_archive_closed_ticket_succeeds(client, admin_key):
    t = _create_ticket(client, admin_key)
    _set_status(client, admin_key, t["id"], "closed")

    r = client.post(f"/tickets/{t['id']}/archive", headers={"X-API-Key": admin_key})
    assert r.status_code == 200, r.text
    assert r.json()["archived"] is True


def test_archive_non_closed_ticket_400(client, admin_key):
    t = _create_ticket(client, admin_key)  # default status "open"
    r = client.post(f"/tickets/{t['id']}/archive", headers={"X-API-Key": admin_key})
    assert r.status_code == 400, r.text
    assert "closed" in r.json()["detail"].lower()


def test_archived_hidden_from_default_list_visible_with_filter(client, admin_key):
    t = _create_ticket(client, admin_key, title="Hide me when archived")
    _set_status(client, admin_key, t["id"], "closed")
    client.post(f"/tickets/{t['id']}/archive", headers={"X-API-Key": admin_key})

    default = client.get("/tickets", headers={"X-API-Key": admin_key})
    assert default.status_code == 200
    assert t["id"] not in [x["id"] for x in default.json()["items"]]

    archived = client.get("/tickets?archived=true", headers={"X-API-Key": admin_key})
    assert archived.status_code == 200
    assert t["id"] in [x["id"] for x in archived.json()["items"]]


def test_unarchive_restores_to_default_list(client, admin_key):
    t = _create_ticket(client, admin_key, title="Bring me back")
    _set_status(client, admin_key, t["id"], "closed")
    client.post(f"/tickets/{t['id']}/archive", headers={"X-API-Key": admin_key})

    r = client.post(f"/tickets/{t['id']}/unarchive", headers={"X-API-Key": admin_key})
    assert r.status_code == 200, r.text
    assert r.json()["archived"] is False

    default = client.get("/tickets", headers={"X-API-Key": admin_key})
    assert t["id"] in [x["id"] for x in default.json()["items"]]


def test_archive_forbidden_for_non_creator_member(client, admin_key, member_key):
    t = _create_ticket(client, admin_key, title="Admin owns this")
    _set_status(client, admin_key, t["id"], "closed")

    r = client.post(f"/tickets/{t['id']}/archive", headers={"X-API-Key": member_key})
    assert r.status_code == 403, r.text


def test_archived_ticket_still_retrievable_directly(client, admin_key):
    t = _create_ticket(client, admin_key, title="Still here")
    _set_status(client, admin_key, t["id"], "closed")
    client.post(f"/tickets/{t['id']}/archive", headers={"X-API-Key": admin_key})

    r = client.get(f"/tickets/{t['id']}", headers={"X-API-Key": admin_key})
    assert r.status_code == 200
    assert r.json()["archived"] is True
