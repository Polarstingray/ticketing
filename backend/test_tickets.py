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


# --- Full-text search (q) ----------------------------------------------------

def test_search_matches_title_and_description_case_insensitive(client, admin_key):
    title_hit = _create(client, admin_key, title="Payment gateway timeout", priority="high")
    desc_hit = _create(
        client, admin_key, title="Unrelated", description="intermittent GATEWAY error", priority="low"
    )
    miss = _create(client, admin_key, title="Totally different", description="nothing here")

    # Lowercase query matches both the title and the (uppercase) description term.
    r = client.get("/tickets?q=gateway", headers={"X-API-Key": admin_key})
    assert r.status_code == 200
    ids = [x["id"] for x in r.json()["items"]]
    assert title_hit["id"] in ids
    assert desc_hit["id"] in ids
    assert miss["id"] not in ids

    # q composes (ANDs) with another filter: same term, narrowed to priority=high.
    r = client.get("/tickets?q=gateway&priority=high", headers={"X-API-Key": admin_key})
    ids = [x["id"] for x in r.json()["items"]]
    assert title_hit["id"] in ids
    assert desc_hit["id"] not in ids

    # Whitespace-only q is ignored (acts as no search filter).
    r = client.get("/tickets?q=%20%20", headers={"X-API-Key": admin_key})
    ids = [x["id"] for x in r.json()["items"]]
    assert miss["id"] in ids


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
    for bad in ("claude:planning", "repo:secret", "dangerous", "fix", "delegate",
                "parent:7", "review-by:7", "rev:deadbeef", "branch:main"):
        r = client.post(
            "/tickets", json={"type": "task", "title": "x", "tags": [bad]},
            headers={"X-API-Key": member.key},
        )
        assert r.status_code == 422, f"{bad}: {r.text}"


def test_member_cannot_set_reserved_tags_on_update(client, make_user):
    member = make_user()
    t = _create(client, member.key, tags=["bug"])
    for bad in ("claude:implementing", "repo:x", "dangerous", "fix", "delegate",
                "parent:7", "review-by:7", "rev:deadbeef", "branch:main"):
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
    monkeypatch.setattr(control_tags, "RESOLVER_BOT_USER_IDS", frozenset({bot.id}))
    t = _create(client, bot.key, tags=["repo:app", "claude:planning"])
    assert _tags(t) == {"repo:app", "claude:planning"}
    r = client.patch(
        f"/tickets/{t['id']}", json={"tags": ["repo:app", "claude:implementing"]},
        headers={"X-API-Key": bot.key},
    )
    assert r.status_code == 200, r.text
    assert _tags(r.json()) == {"repo:app", "claude:implementing"}


def test_resolver_bot_flag_can_set_reserved_tags(client, make_user):
    """A non-admin user flagged is_resolver_bot may manage control tags even when
    RESOLVER_BOT_USER_IDS is empty — the DB flag is authoritative, so there is no
    RESOLVER_BOT_USER_ID env to keep in sync."""
    from database import SessionLocal
    from models import User
    bot = make_user()
    db = SessionLocal()
    try:
        db.query(User).filter(User.id == bot.id).update({"is_resolver_bot": True})
        db.commit()
    finally:
        db.close()
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


# --- Tags: `cli`-scoped API keys ---------------------------------------------
# A scope is carried by the *key*, not the user, and unlocks only the reserved
# prefixes that *aim* the resolver (`repo:`, `rev:`, `branch:`). These tests pin that
# boundary: the scope must not leak to any other control tag, and must not leak to the
# same user's cookie session.

def test_cli_scoped_key_can_set_repo_tag(client, make_user, scoped_key):
    member = make_user()
    key = scoped_key(member.id)
    t = _create(client, key, tags=["repo:app", "backend"])
    assert _tags(t) == {"repo:app", "backend"}


def test_cli_scoped_key_can_pin_a_commit(client, make_user, scoped_key):
    """`stingray review` records the commit it was filed against, so the resolver
    reviews that code instead of whatever the checkout is sitting on."""
    member = make_user()
    key = scoped_key(member.id)
    t = _create(client, key, tags=["repo:app", "rev:" + "a" * 40, "branch:feat/probe"])
    assert _tags(t) == {"repo:app", "rev:" + "a" * 40, "branch:feat/probe"}


def test_cli_scoped_key_cannot_set_other_reserved_tags(client, make_user, scoped_key):
    """The whole point of scoping: `repo:` only, never a workflow or safety tag."""
    member = make_user()
    key = scoped_key(member.id)
    for bad in ("claude:planning", "dangerous", "fix", "delegate", "parent:7", "review-by:7"):
        r = client.post(
            "/tickets", json={"type": "task", "title": "x", "tags": [bad]},
            headers={"X-API-Key": key},
        )
        assert r.status_code == 422, f"{bad}: {r.text}"


def test_unscoped_key_still_cannot_pin_a_commit(client, make_user):
    """A pin aims the resolver at code to change, so it needs the same trust as
    `repo:` — an ordinary key must not be able to redirect a fix."""
    member = make_user()
    for bad in ("rev:" + "a" * 40, "branch:main"):
        r = client.post(
            "/tickets", json={"type": "task", "title": "x", "tags": ["bug", bad]},
            headers={"X-API-Key": member.key},
        )
        assert r.status_code == 422, f"{bad}: {r.text}"


def test_unscoped_key_still_cannot_set_repo_tag(client, make_user):
    """Regression: granting the scope to *some* keys must not relax the default."""
    member = make_user()
    r = client.post(
        "/tickets", json={"type": "task", "title": "x", "tags": ["repo:app"]},
        headers={"X-API-Key": member.key},
    )
    assert r.status_code == 422


def test_cli_scope_does_not_leak_to_cookie_session(client, make_user, scoped_key):
    """The scope rides the key. Logging in as the same user must not inherit it —
    if it did, `request.state.api_key` would be leaking across requests."""
    member = make_user()
    scoped_key(member.id)  # same user owns a cli key...
    login = client.post("/auth/login",
                        json={"username": member.username, "password": member.password})
    assert login.status_code == 200, login.text
    r = client.post("/tickets", json={"type": "task", "title": "x", "tags": ["repo:app"]})
    assert r.status_code == 422, r.text
    client.cookies.clear()


def test_cli_scoped_key_can_correct_its_own_repo_tag(client, make_user, scoped_key):
    """A caller allowed to SET a reserved tag must also be able to change it —
    the old blanket "preserve all reserved tags" rule made repo: write-once."""
    member = make_user()
    key = scoped_key(member.id)
    t = _create(client, key, tags=["repo:app"])
    r = client.patch(f"/tickets/{t['id']}", json={"tags": ["repo:other"]},
                     headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    assert _tags(r.json()) == {"repo:other"}


def test_cli_scope_does_not_unpin_other_reserved_tags(client, admin_key, make_user, scoped_key):
    """A scoped key edits its own repo: tag but must not strip a control tag."""
    member = make_user()
    key = scoped_key(member.id)
    t = _create(client, key, tags=["repo:app"])
    r = client.patch(f"/tickets/{t['id']}", json={"tags": ["repo:app", "claude:planning"]},
                     headers={"X-API-Key": admin_key})
    assert r.status_code == 200, r.text

    # The member's scoped key changes repo: and drops the free tags; claude:* stays.
    r = client.patch(f"/tickets/{t['id']}", json={"tags": ["repo:new"]},
                     headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    assert _tags(r.json()) == {"repo:new", "claude:planning"}


def test_free_epic_tag_needs_no_scope(client, make_user):
    """Scaffold grouping uses a free `epic:` tag precisely so it needs no scope
    (and so it never triggers the resolver's self-driving `parent:` behavior)."""
    member = make_user()
    t = _create(client, member.key, tags=["epic:7", "scaffold"])
    assert _tags(t) == {"epic:7", "scaffold"}


# --- Bulk update --------------------------------------------------------------

def test_bulk_update_status_happy_path(client, admin_key):
    t1 = _create(client, admin_key, title="bulk a")
    t2 = _create(client, admin_key, title="bulk b")

    r = client.post(
        "/tickets/bulk-update",
        json={"ids": [t1["id"], t2["id"]], "status": "resolved"},
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"updated", "failed"}
    updated_ids = {t["id"] for t in body["updated"]}
    assert t1["id"] in updated_ids
    assert t2["id"] in updated_ids
    assert all(t["status"] == "resolved" for t in body["updated"])
    assert body["failed"] == []


def test_bulk_update_skips_tickets_caller_cannot_modify(client, admin_key, make_user):
    member = make_user()
    # admin owns this; member cannot modify it
    admin_ticket = _create(client, admin_key, title="admin only")
    # member owns this
    member_ticket = _create(client, member.key, title="member owns")

    r = client.post(
        "/tickets/bulk-update",
        json={"ids": [admin_ticket["id"], member_ticket["id"]], "status": "resolved"},
        headers={"X-API-Key": member.key},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    updated_ids = {t["id"] for t in body["updated"]}
    failed_ids = {f["id"] for f in body["failed"]}
    assert member_ticket["id"] in updated_ids
    assert admin_ticket["id"] in failed_ids


def test_bulk_update_rejects_empty_ids(client, admin_key):
    r = client.post(
        "/tickets/bulk-update",
        json={"ids": [], "status": "resolved"},
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 422


def test_bulk_update_requires_at_least_one_field(client, admin_key):
    t = _create(client, admin_key)
    r = client.post(
        "/tickets/bulk-update",
        json={"ids": [t["id"]]},
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 400


def test_bulk_update_nonexistent_ids_go_to_failed(client, admin_key):
    t = _create(client, admin_key, title="exists")
    r = client.post(
        "/tickets/bulk-update",
        json={"ids": [t["id"], 999999], "status": "closed"},
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    updated_ids = {u["id"] for u in body["updated"]}
    failed_ids = {f["id"] for f in body["failed"]}
    assert t["id"] in updated_ids
    assert 999999 in failed_ids
