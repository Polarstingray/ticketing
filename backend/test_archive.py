"""Tests for ticket archive / unarchive.

Uses the shared fixtures in conftest.py (isolated DB, seeded admin, API keys,
rate-limit disabled). Assertions check membership/flags rather than absolute
counts because the suite shares one database.
"""


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


def test_archive_forbidden_for_non_creator_member(client, admin_key, make_user):
    t = _create_ticket(client, admin_key, title="Admin owns this")
    _set_status(client, admin_key, t["id"], "closed")

    member = make_user()
    r = client.post(
        f"/tickets/{t['id']}/archive", headers={"X-API-Key": member.key}
    )
    # The archive endpoint checks modify permission directly -> 403 for a member
    # who is neither creator nor assignee.
    assert r.status_code == 403, r.text


def test_archived_ticket_still_retrievable_directly(client, admin_key):
    t = _create_ticket(client, admin_key, title="Still here")
    _set_status(client, admin_key, t["id"], "closed")
    client.post(f"/tickets/{t['id']}/archive", headers={"X-API-Key": admin_key})

    r = client.get(f"/tickets/{t['id']}", headers={"X-API-Key": admin_key})
    assert r.status_code == 200
    assert r.json()["archived"] is True
