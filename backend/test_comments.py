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
    assert "first comment" in [c["body"] for c in listed.json()]


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
