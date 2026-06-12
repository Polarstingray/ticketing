"""Core ticket flows: CRUD, the pagination envelope, and IDOR enforcement.

Uses the shared conftest fixtures. The suite shares one database, so list
assertions check membership by id rather than absolute counts.
"""

from datetime import datetime


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


def test_timestamps_serialized_as_utc_aware(client, admin_key):
    """Timestamps must carry an explicit UTC offset so clients don't reinterpret
    a naive (local-looking) string and skew the displayed time (#24)."""
    t = _create(client, admin_key, due_date="2026-06-07T01:23:45")
    for field in ("created_at", "updated_at", "due_date"):
        val = t[field]
        assert val.endswith(("Z", "+00:00")), f"{field} not UTC-aware: {val!r}"
        parsed = datetime.fromisoformat(val.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None


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


# --- Tags: editing & reserved-tag security (#58) -----------------------------

def _tags(t):
    return set(t["tags"])


def test_member_can_edit_free_tags_on_own_ticket(client, make_user):
    member = make_user()
    t = _create(client, member.key, tags=["bug"])
    r = client.patch(
        f"/tickets/{t['id']}", json={"tags": ["bug", "urgent"]},
        headers={"X-API-Key": member.key},
    )
    assert r.status_code == 200, r.text
    assert _tags(r.json()) == {"bug", "urgent"}

    # Removing a free tag works too.
    r = client.patch(
        f"/tickets/{t['id']}", json={"tags": ["urgent"]},
        headers={"X-API-Key": member.key},
    )
    assert r.status_code == 200
    assert _tags(r.json()) == {"urgent"}

    act = client.get(f"/tickets/{t['id']}/activity", headers={"X-API-Key": member.key})
    assert "tags_changed" in [a["action"] for a in act.json()]


def test_member_cannot_set_reserved_tags_on_create(client, make_user):
    member = make_user()
    for bad in ("claude:planning", "repo:secret", "dangerous", "fix"):
        r = client.post(
            "/tickets", json={"type": "task", "title": "x", "tags": [bad]},
            headers={"X-API-Key": member.key},
        )
        assert r.status_code == 422, f"{bad}: {r.text}"


def test_member_cannot_set_reserved_tags_on_update(client, make_user):
    member = make_user()
    t = _create(client, member.key, tags=["bug"])
    for bad in ("claude:implementing", "repo:x", "dangerous", "fix"):
        r = client.patch(
            f"/tickets/{t['id']}", json={"tags": ["bug", bad]},
            headers={"X-API-Key": member.key},
        )
        assert r.status_code == 422, f"{bad}: {r.text}"
    # The ticket's tags are unchanged.
    cur = client.get(f"/tickets/{t['id']}", headers={"X-API-Key": member.key})
    assert _tags(cur.json()) == {"bug"}


def test_existing_reserved_tags_preserved_when_member_edits_free_tags(client, admin_key, make_user):
    member = make_user()
    t = _create(client, member.key, tags=["bug"])
    # Admin sets reserved control tags.
    r = client.patch(
        f"/tickets/{t['id']}", json={"tags": ["bug", "claude:planning", "repo:app", "dangerous"]},
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 200, r.text
    assert _tags(r.json()) == {"bug", "claude:planning", "repo:app", "dangerous"}

    # Member edits free tags; reserved tags survive untouched even though the
    # member's payload omits them.
    r = client.patch(
        f"/tickets/{t['id']}", json={"tags": ["feature"]},
        headers={"X-API-Key": member.key},
    )
    assert r.status_code == 200, r.text
    assert _tags(r.json()) == {"feature", "claude:planning", "repo:app", "dangerous"}


def test_admin_can_set_reserved_tags(client, admin_key):
    t = _create(client, admin_key)
    r = client.patch(
        f"/tickets/{t['id']}", json={"tags": ["claude:awaiting-pr-review", "repo:app"]},
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 200, r.text
    assert _tags(r.json()) == {"claude:awaiting-pr-review", "repo:app"}


def test_resolver_bot_can_set_reserved_tags(client, make_user, monkeypatch):
    """A non-admin user whose id == RESOLVER_BOT_USER_ID may transition control
    tags — this is what keeps the resolver's set_state working."""
    import control_tags
    bot = make_user()
    monkeypatch.setattr(control_tags, "RESOLVER_BOT_USER_ID", bot.id)
    t = _create(client, bot.key, tags=["repo:app", "claude:planning"])
    assert _tags(t) == {"repo:app", "claude:planning"}
    r = client.patch(
        f"/tickets/{t['id']}", json={"tags": ["repo:app", "claude:implementing"]},
        headers={"X-API-Key": bot.key},
    )
    assert r.status_code == 200, r.text
    assert _tags(r.json()) == {"repo:app", "claude:implementing"}


def test_tag_validation_rejects_bad_payloads(client, admin_key):
    t = _create(client, admin_key)
    # Too many tags.
    r = client.patch(f"/tickets/{t['id']}", json={"tags": [f"t{i}" for i in range(31)]},
                     headers={"X-API-Key": admin_key})
    assert r.status_code == 422
    # Over-length tag.
    r = client.patch(f"/tickets/{t['id']}", json={"tags": ["x" * 51]},
                     headers={"X-API-Key": admin_key})
    assert r.status_code == 422
    # Control character / newline (prompt-injection vector).
    r = client.patch(f"/tickets/{t['id']}", json={"tags": ["evil\ntag"]},
                     headers={"X-API-Key": admin_key})
    assert r.status_code == 422


def test_non_member_cannot_edit_tags(client, admin_key, make_user):
    t = _create(client, admin_key, title="secret")
    member = make_user()
    r = client.patch(f"/tickets/{t['id']}", json={"tags": ["x"]},
                     headers={"X-API-Key": member.key})
    assert r.status_code == 403
