"""Comment flows and IDOR enforcement on the nested comments endpoint."""


def _create_ticket(client, key, **overrides):
    body = {"type": "task", "title": "Commentable"}
    body.update(overrides)
    r = client.post("/tickets", json=body, headers={"X-API-Key": key})
    assert r.status_code == 201, r.text
    return r.json()


def _create_comment(client, ticket_id, key, **overrides):
    body = {"body": "new comment"}
    body.update(overrides)
    r = client.post(f"/tickets/{ticket_id}/comments", json=body, headers={"X-API-Key": key})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_and_list_comment(client, admin_key):
    t = _create_ticket(client, admin_key)
    c = _create_comment(client, t["id"], admin_key, body="first comment")
    assert c["body"] == "first comment"

    listed = client.get(f"/tickets/{t['id']}/comments", headers={"X-API-Key": admin_key})
    assert listed.status_code == 200
    assert "first comment" in [c["body"] for c in listed.json()["items"]]


def test_empty_comment_rejected(client, admin_key):
    t = _create_ticket(client, admin_key)
    r = client.post(
        f"/tickets/{t['id']}/comments", json={"body": ""}, headers={"X-API-Key": admin_key}
    )
    assert r.status_code == 422


def test_member_cannot_comment_on_unseen_ticket(client, admin_key, make_user):
    t = _create_ticket(client, admin_key, title="private")
    member = make_user()
    # 404 (not 403) so existence isn't confirmed.
    r = client.post(
        f"/tickets/{t['id']}/comments", json={"body": "sneaky"},
        headers={"X-API-Key": member.key},
    )
    assert r.status_code == 404


def test_member_cannot_list_comments_on_unseen_ticket(client, admin_key, make_user):
    t = _create_ticket(client, admin_key, title="private")
    client.post(
        f"/tickets/{t['id']}/comments", json={"body": "hush"}, headers={"X-API-Key": admin_key}
    )
    member = make_user()
    r = client.get(f"/tickets/{t['id']}/comments", headers={"X-API-Key": member.key})
    assert r.status_code == 404


def test_assignee_can_comment(client, admin_key, make_user):
    member = make_user()
    t = _create_ticket(client, admin_key, assigned_to=member.id)
    r = client.post(
        f"/tickets/{t['id']}/comments", json={"body": "on it"},
        headers={"X-API-Key": member.key},
    )
    assert r.status_code == 201, r.text


def test_author_can_edit_comment(client, admin_key, make_user):
    member = make_user()
    t = _create_ticket(client, admin_key, assigned_to=member.id)
    c = _create_comment(client, t["id"], member.key, body="typo")
    r = client.patch(
        f"/tickets/{t['id']}/comments/{c['id']}", json={"body": "fixed"},
        headers={"X-API-Key": member.key},
    )
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "fixed"
    assert r.json()["id"] == c["id"]


def test_author_can_delete_comment(client, admin_key, make_user):
    member = make_user()
    t = _create_ticket(client, admin_key, assigned_to=member.id)
    c = _create_comment(client, t["id"], member.key, body="oops")
    r = client.delete(
        f"/tickets/{t['id']}/comments/{c['id']}", headers={"X-API-Key": member.key},
    )
    assert r.status_code == 204, r.text
    listed = client.get(f"/tickets/{t['id']}/comments", headers={"X-API-Key": member.key})
    assert c["id"] not in [x["id"] for x in listed.json()["items"]]


def test_non_author_with_view_access_gets_403(client, admin_key, make_user):
    member = make_user()
    # admin authors a comment on a ticket the member can view (assigned to them);
    # member is not the author and not admin -> 403.
    t = _create_ticket(client, admin_key, assigned_to=member.id)
    c = _create_comment(client, t["id"], admin_key, body="admin note")
    r = client.patch(
        f"/tickets/{t['id']}/comments/{c['id']}", json={"body": "nope"},
        headers={"X-API-Key": member.key},
    )
    assert r.status_code == 403, r.text
    r = client.delete(
        f"/tickets/{t['id']}/comments/{c['id']}", headers={"X-API-Key": member.key},
    )
    assert r.status_code == 403, r.text


def test_admin_can_edit_and_delete_others_comment(client, admin_key, make_user):
    member = make_user()
    t = _create_ticket(client, admin_key, assigned_to=member.id)
    c = _create_comment(client, t["id"], member.key, body="member's")
    r = client.patch(
        f"/tickets/{t['id']}/comments/{c['id']}", json={"body": "edited by admin"},
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "edited by admin"
    r = client.delete(
        f"/tickets/{t['id']}/comments/{c['id']}", headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 204, r.text


def test_edit_missing_comment_404(client, admin_key):
    t = _create_ticket(client, admin_key)
    r = client.patch(
        f"/tickets/{t['id']}/comments/999999", json={"body": "ghost"},
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 404, r.text


def test_delete_missing_comment_404(client, admin_key):
    t = _create_ticket(client, admin_key)
    r = client.delete(
        f"/tickets/{t['id']}/comments/999999", headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 404, r.text


# --- Pagination tests ---------------------------------------------------------

def test_list_comments_returns_paginated_envelope(client, admin_key):
    t = _create_ticket(client, admin_key)
    for i in range(3):
        _create_comment(client, t["id"], admin_key, body=f"comment {i}")
    r = client.get(f"/tickets/{t['id']}/comments", headers={"X-API-Key": admin_key})
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "total" in data
    assert "limit" in data
    assert "offset" in data
    assert data["total"] == 3
    assert len(data["items"]) == 3


def test_pagination_limit_and_offset(client, admin_key):
    t = _create_ticket(client, admin_key)
    for i in range(5):
        _create_comment(client, t["id"], admin_key, body=f"comment {i}")

    r1 = client.get(
        f"/tickets/{t['id']}/comments?limit=2&offset=0", headers={"X-API-Key": admin_key}
    )
    assert r1.status_code == 200
    data1 = r1.json()
    assert len(data1["items"]) == 2
    assert data1["total"] == 5

    r2 = client.get(
        f"/tickets/{t['id']}/comments?limit=2&offset=2", headers={"X-API-Key": admin_key}
    )
    assert r2.status_code == 200
    data2 = r2.json()
    assert len(data2["items"]) == 2
    # Items on page 2 should differ from page 1
    ids1 = {c["id"] for c in data1["items"]}
    ids2 = {c["id"] for c in data2["items"]}
    assert ids1.isdisjoint(ids2)


def test_page1_cut_short_by_long_comment(client, admin_key):
    """Page 1 stops after the first comment whose body exceeds 500 chars."""
    t = _create_ticket(client, admin_key)
    _create_comment(client, t["id"], admin_key, body="short before")
    _create_comment(client, t["id"], admin_key, body="x" * 501)
    _create_comment(client, t["id"], admin_key, body="should not appear on page 1")

    r = client.get(f"/tickets/{t['id']}/comments", headers={"X-API-Key": admin_key})
    assert r.status_code == 200
    data = r.json()
    # total reflects all comments in DB
    assert data["total"] == 3
    # Only the first two comments are returned on page 1 (short + the long one)
    assert len(data["items"]) == 2
    bodies = [c["body"] for c in data["items"]]
    assert "short before" in bodies
    assert "x" * 501 in bodies
    assert "should not appear on page 1" not in bodies


def test_page1_long_comment_is_first(client, admin_key):
    """When the very first comment is long, page 1 returns only that comment."""
    t = _create_ticket(client, admin_key)
    _create_comment(client, t["id"], admin_key, body="y" * 600)
    _create_comment(client, t["id"], admin_key, body="second")

    r = client.get(f"/tickets/{t['id']}/comments", headers={"X-API-Key": admin_key})
    data = r.json()
    assert data["total"] == 2
    assert len(data["items"]) == 1
    assert data["items"][0]["body"] == "y" * 600


def test_subsequent_page_returns_long_comments(client, admin_key):
    """With offset > 0 the body-length cutoff does NOT apply."""
    t = _create_ticket(client, admin_key)
    _create_comment(client, t["id"], admin_key, body="first")
    _create_comment(client, t["id"], admin_key, body="z" * 501)
    _create_comment(client, t["id"], admin_key, body="third")

    r = client.get(
        f"/tickets/{t['id']}/comments?limit=10&offset=1", headers={"X-API-Key": admin_key}
    )
    data = r.json()
    assert data["total"] == 3
    # Items at offset 1 include the long comment and the one after it
    assert len(data["items"]) == 2


def test_list_comments_empty_ticket(client, admin_key):
    """A ticket with no comments returns an empty paginated envelope."""
    t = _create_ticket(client, admin_key)
    r = client.get(f"/tickets/{t['id']}/comments", headers={"X-API-Key": admin_key})
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 0
    assert len(data["items"]) == 0
    assert data["offset"] == 0
