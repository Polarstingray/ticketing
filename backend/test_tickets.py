"""Core ticket flows: CRUD, the pagination envelope, and IDOR enforcement.

Uses the shared conftest fixtures. The suite shares one database, so list
assertions check membership by id rather than absolute counts.
"""


def _create(client, key, **overrides):
    body = {"type": "task", "title": "A ticket"}
    body.update(overrides)
    r = client.post("/tickets", json=body, headers={"X-API-Key": key})
    assert r.status_code == 201, r.text
    return r.json()


# --- CRUD --------------------------------------------------------------------

def test_create_and_get_ticket(client, admin_key):
    t = _create(client, admin_key, title="Fix the thing", priority="high")
    assert t["title"] == "Fix the thing"
    assert t["priority"] == "high"
    assert t["status"] == "open"
    assert t["archived"] is False

    r = client.get(f"/tickets/{t['id']}", headers={"X-API-Key": admin_key})
    assert r.status_code == 200
    assert r.json()["id"] == t["id"]


def test_create_requires_title(client, admin_key):
    r = client.post("/tickets", json={"type": "task", "title": ""}, headers={"X-API-Key": admin_key})
    assert r.status_code == 422


def test_get_missing_ticket_404(client, admin_key):
    r = client.get("/tickets/999999", headers={"X-API-Key": admin_key})
    assert r.status_code == 404


def test_patch_updates_fields_and_records_activity(client, admin_key):
    t = _create(client, admin_key)
    r = client.patch(
        f"/tickets/{t['id']}",
        json={"status": "in_review", "priority": "low"},
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "in_review"
    assert r.json()["priority"] == "low"

    act = client.get(f"/tickets/{t['id']}/activity", headers={"X-API-Key": admin_key})
    assert act.status_code == 200
    actions = [a["action"] for a in act.json()]
    assert "created" in actions
    assert "status_changed" in actions


def test_delete_ticket_admin_only(client, admin_key, make_user):
    t = _create(client, admin_key)
    member = make_user()

    # A member assigned to the ticket still can't delete it (admin-only).
    client.patch(
        f"/tickets/{t['id']}", json={"assigned_to": member.id}, headers={"X-API-Key": admin_key}
    )
    forbidden = client.delete(f"/tickets/{t['id']}", headers={"X-API-Key": member.key})
    assert forbidden.status_code == 403

    ok = client.delete(f"/tickets/{t['id']}", headers={"X-API-Key": admin_key})
    assert ok.status_code == 204
    assert client.get(f"/tickets/{t['id']}", headers={"X-API-Key": admin_key}).status_code == 404


# --- Pagination --------------------------------------------------------------

def test_pagination_envelope(client, admin_key):
    for _ in range(3):
        _create(client, admin_key, title="paged")
    r = client.get("/tickets?limit=2&offset=0", headers={"X-API-Key": admin_key})
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"items", "total", "limit", "offset"}
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) <= 2
    assert body["total"] >= 3


def test_pagination_limit_bounds_enforced(client, admin_key):
    assert client.get("/tickets?limit=0", headers={"X-API-Key": admin_key}).status_code == 422
    assert client.get("/tickets?limit=999", headers={"X-API-Key": admin_key}).status_code == 422


# --- IDOR / visibility -------------------------------------------------------

def test_member_cannot_view_others_ticket(client, admin_key, make_user):
    """A member who is neither creator nor assignee gets 404 (not 403, so the
    ticket's existence isn't confirmed)."""
    t = _create(client, admin_key, title="secret")
    member = make_user()
    r = client.get(f"/tickets/{t['id']}", headers={"X-API-Key": member.key})
    assert r.status_code == 404


def test_member_list_only_shows_own_tickets(client, admin_key, make_user):
    other = _create(client, admin_key, title="admin owns")
    member = make_user()
    mine = _create(client, member.key, title="member owns")

    r = client.get("/tickets", headers={"X-API-Key": member.key})
    assert r.status_code == 200
    ids = [x["id"] for x in r.json()["items"]]
    assert mine["id"] in ids
    assert other["id"] not in ids


def test_member_can_view_assigned_ticket(client, admin_key, make_user):
    member = make_user()
    t = _create(client, admin_key, title="assigned", assigned_to=member.id)
    r = client.get(f"/tickets/{t['id']}", headers={"X-API-Key": member.key})
    assert r.status_code == 200
    assert r.json()["id"] == t["id"]


def test_unauthenticated_request_rejected(client):
    assert client.get("/tickets").status_code == 401
    assert client.get("/tickets", headers={"X-API-Key": "sk_not-a-real-key"}).status_code == 401
