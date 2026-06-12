"""Tests for in-app notifications (ticket #25).

Covers: assigning a ticket notifies the assignee (not the assigner); commenting
notifies the assignee + creator (not the commenter); unread_count / read /
read_all bookkeeping; and that delete / bulk_delete and single-row access are
strictly scoped to the calling user.
"""
import os
import tempfile

import pytest

# Isolated SQLite file per test session, set before importing app modules.
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

    limiter.enabled = False  # one client IP logs in many times across the suite
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_admin(db)
    finally:
        db.close()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def admin_key():
    """Mint a fresh API key for the seeded admin (its plaintext isn't stored)."""
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


@pytest.fixture
def make_user(client, admin_key):
    """Factory: create a member and return (user_id, api_key) authed as them."""
    created = []

    def _make(name):
        # Namespace usernames: the test DB engine is shared across test modules
        # (it's bound at first import), so a bare "bob" would collide with names
        # other suites create. The prefix keeps these rows unique.
        username = f"notif_{name}"
        r = client.post(
            "/users",
            headers={"X-API-Key": admin_key},
            json={
                "username": username,
                "display_name": name.title(),
                "email": f"{username}@example.com",
                "password": "pw1pw1",
                "role": "member",
            },
        )
        assert r.status_code == 201, r.text
        uid = r.json()["id"]
        # Mint an API key for the member directly in the DB.
        db = SessionLocal()
        try:
            raw = auth.generate_api_key()
            db.add(ApiKey(
                user_id=uid, name="test", key_prefix=raw[:11],
                key_hash=auth.hash_api_key(raw),
            ))
            db.commit()
        finally:
            db.close()
        created.append(uid)
        return uid, raw

    return _make


def _hdr(key):
    return {"X-API-Key": key}


def _create_ticket(client, key, **kw):
    body = {"type": "task", "title": "T", **kw}
    r = client.post("/tickets", headers=_hdr(key), json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --- Assignment notifications ------------------------------------------------

def test_assign_notifies_assignee_not_assigner(client, admin_key, make_user):
    alice_id, alice_key = make_user("alice")
    # The suite shares one admin across modules, so other tests may have left it
    # notifications; snapshot its unread count and assert this action adds none.
    admin_unread_before = client.get(
        "/notifications/unread_count", headers=_hdr(admin_key)
    ).json()["unread_count"]
    # Admin creates a ticket assigned to alice.
    _create_ticket(client, admin_key, title="Assigned at create", assigned_to=alice_id)

    r = client.get("/notifications", headers=_hdr(alice_key))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["unread_count"] == 1
    assert len(data["items"]) == 1
    n = data["items"][0]
    assert n["type"] == "assigned"
    assert n["ticket_title"] == "Assigned at create"
    assert n["actor_name"] == "admin"  # seeded admin display name
    assert n["read"] is False

    # The assigner (admin) gets nothing from this assignment.
    r = client.get("/notifications/unread_count", headers=_hdr(admin_key))
    assert r.json()["unread_count"] == admin_unread_before


def test_assign_via_update_notifies(client, admin_key, make_user):
    bob_id, bob_key = make_user("bob")
    ticket = _create_ticket(client, admin_key, title="Later assigned")
    r = client.patch(
        f"/tickets/{ticket['id']}", headers=_hdr(admin_key),
        json={"assigned_to": bob_id},
    )
    assert r.status_code == 200, r.text
    assert client.get("/notifications/unread_count", headers=_hdr(bob_key)).json()["unread_count"] == 1


def test_self_assign_does_not_notify(client, admin_key):
    # Admin assigns a ticket to themselves -> no notification.
    admin = SessionLocal().query(User).filter(User.username == "admin").first()
    _create_ticket(client, admin_key, title="Self", assigned_to=admin.id)
    # Filter to assigned notifications about this; admin should still have 0.
    r = client.get("/notifications", headers=_hdr(admin_key))
    titles = [n["ticket_title"] for n in r.json()["items"]]
    assert "Self" not in titles


# --- Comment notifications ---------------------------------------------------

def test_comment_notifies_creator_and_assignee_not_commenter(client, admin_key, make_user):
    carol_id, carol_key = make_user("carol")
    dave_id, dave_key = make_user("dave")
    # Carol creates a ticket, assigned to Dave.
    ticket = _create_ticket(client, carol_key, title="Discuss", assigned_to=dave_id)

    # Clear the assignment notification Dave already has, to isolate the comment.
    client.post("/notifications/read_all", headers=_hdr(dave_key))

    # Admin (a third party) comments -> both Carol (creator) and Dave (assignee) notified.
    r = client.post(
        f"/tickets/{ticket['id']}/comments", headers=_hdr(admin_key),
        json={"body": "hello"},
    )
    assert r.status_code == 201, r.text

    carol = client.get("/notifications", headers=_hdr(carol_key)).json()
    dave = client.get("/notifications", headers=_hdr(dave_key)).json()
    assert any(n["type"] == "commented" for n in carol["items"])
    assert any(n["type"] == "commented" for n in dave["items"])

    # The commenter (admin) is not notified about their own comment. Scope the
    # check to this ticket: the admin is shared across the suite and may carry
    # "commented" rows from other modules' tickets.
    admin = client.get("/notifications", headers=_hdr(admin_key)).json()
    assert not any(
        n["type"] == "commented" and n["ticket_id"] == ticket["id"]
        for n in admin["items"]
    )


def test_commenter_who_is_involved_not_self_notified(client, admin_key, make_user):
    erin_id, erin_key = make_user("erin")
    # Erin creates + is assigned, then comments herself -> no self-notification.
    ticket = _create_ticket(client, erin_key, title="Solo", assigned_to=erin_id)
    client.post("/notifications/read_all", headers=_hdr(erin_key))
    client.post(f"/tickets/{ticket['id']}/comments", headers=_hdr(erin_key), json={"body": "note"})
    assert client.get("/notifications/unread_count", headers=_hdr(erin_key)).json()["unread_count"] == 0


# --- Read / unread bookkeeping -----------------------------------------------

def test_mark_read_and_read_all(client, admin_key, make_user):
    frank_id, frank_key = make_user("frank")
    _create_ticket(client, admin_key, title="One", assigned_to=frank_id)
    _create_ticket(client, admin_key, title="Two", assigned_to=frank_id)

    data = client.get("/notifications", headers=_hdr(frank_key)).json()
    assert data["unread_count"] == 2
    first_id = data["items"][0]["id"]

    r = client.post(f"/notifications/{first_id}/read", headers=_hdr(frank_key))
    assert r.status_code == 200
    assert r.json()["read"] is True
    assert client.get("/notifications/unread_count", headers=_hdr(frank_key)).json()["unread_count"] == 1

    r = client.post("/notifications/read_all", headers=_hdr(frank_key))
    assert r.json()["unread_count"] == 0
    assert client.get("/notifications/unread_count", headers=_hdr(frank_key)).json()["unread_count"] == 0


def test_unread_filter(client, admin_key, make_user):
    gina_id, gina_key = make_user("gina")
    _create_ticket(client, admin_key, title="U1", assigned_to=gina_id)
    _create_ticket(client, admin_key, title="U2", assigned_to=gina_id)
    items = client.get("/notifications", headers=_hdr(gina_key)).json()["items"]
    one = items[0]["id"]
    client.post(f"/notifications/{one}/read", headers=_hdr(gina_key))

    unread_only = client.get("/notifications?unread=true", headers=_hdr(gina_key)).json()
    assert all(n["read"] is False for n in unread_only["items"])
    assert unread_only["total"] == 1
    read_only = client.get("/notifications?unread=false", headers=_hdr(gina_key)).json()
    assert read_only["total"] == 1
    assert all(n["read"] is True for n in read_only["items"])


# --- Deletion (single + bulk) and per-user scoping ---------------------------

def test_delete_single(client, admin_key, make_user):
    hugo_id, hugo_key = make_user("hugo")
    _create_ticket(client, admin_key, title="Del", assigned_to=hugo_id)
    nid = client.get("/notifications", headers=_hdr(hugo_key)).json()["items"][0]["id"]
    assert client.delete(f"/notifications/{nid}", headers=_hdr(hugo_key)).status_code == 204
    assert client.get("/notifications", headers=_hdr(hugo_key)).json()["total"] == 0


def test_bulk_delete_selected(client, admin_key, make_user):
    ivy_id, ivy_key = make_user("ivy")
    for t in ("A", "B", "C"):
        _create_ticket(client, admin_key, title=t, assigned_to=ivy_id)
    ids = [n["id"] for n in client.get("/notifications", headers=_hdr(ivy_key)).json()["items"]]
    r = client.post("/notifications/bulk_delete", headers=_hdr(ivy_key), json={"ids": ids[:2]})
    assert r.json()["deleted"] == 2
    assert client.get("/notifications", headers=_hdr(ivy_key)).json()["total"] == 1


def test_bulk_delete_all(client, admin_key, make_user):
    jane_id, jane_key = make_user("jane")
    for t in ("A", "B"):
        _create_ticket(client, admin_key, title=t, assigned_to=jane_id)
    r = client.post("/notifications/bulk_delete", headers=_hdr(jane_key), json={"all": True})
    assert r.json()["deleted"] == 2
    assert client.get("/notifications", headers=_hdr(jane_key)).json()["total"] == 0


def test_cannot_access_another_users_notification(client, admin_key, make_user):
    ken_id, ken_key = make_user("ken")
    liz_id, liz_key = make_user("liz")
    _create_ticket(client, admin_key, title="Kens", assigned_to=ken_id)
    nid = client.get("/notifications", headers=_hdr(ken_key)).json()["items"][0]["id"]

    # Liz cannot read or delete Ken's notification.
    assert client.post(f"/notifications/{nid}/read", headers=_hdr(liz_key)).status_code == 404
    assert client.delete(f"/notifications/{nid}", headers=_hdr(liz_key)).status_code == 404

    # Bulk delete with Ken's id from Liz's session deletes nothing of Ken's.
    r = client.post("/notifications/bulk_delete", headers=_hdr(liz_key), json={"ids": [nid]})
    assert r.json()["deleted"] == 0
    # Ken's notification survives.
    assert client.get("/notifications", headers=_hdr(ken_key)).json()["total"] == 1


def test_other_users_notifications_excluded_from_list(client, admin_key, make_user):
    mary_id, mary_key = make_user("mary")
    nina_id, nina_key = make_user("nina")
    _create_ticket(client, admin_key, title="MaryOnly", assigned_to=mary_id)
    nina_data = client.get("/notifications", headers=_hdr(nina_key)).json()
    assert all(n["ticket_title"] != "MaryOnly" for n in nina_data["items"])
