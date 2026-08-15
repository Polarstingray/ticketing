"""User management and API-key lifecycle.

Covers admin-only user creation/listing, the self-or-admin rule on API keys,
and that a revoked key stops authenticating.
"""


def test_admin_can_create_and_list_users(client, admin_key):
    r = client.post(
        "/users",
        headers={"X-API-Key": admin_key},
        json={
            "username": "newbie",
            "display_name": "Newbie",
            "email": "newbie@example.com",
            "password": "pw1pw1",
            "role": "member",
        },
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]

    listed = client.get("/users", headers={"X-API-Key": admin_key})
    assert listed.status_code == 200
    assert uid in [u["id"] for u in listed.json()]


def test_create_user_short_password_rejected(client, admin_key):
    r = client.post(
        "/users",
        headers={"X-API-Key": admin_key},
        json={
            "username": "shorty",
            "display_name": "Shorty",
            "email": "shorty@example.com",
            "password": "abc",
            "role": "member",
        },
    )
    assert r.status_code == 422


def test_duplicate_username_rejected(client, admin_key):
    body = {
        "username": "dupe",
        "display_name": "Dupe",
        "email": "dupe@example.com",
        "password": "pw1pw1",
        "role": "member",
    }
    assert client.post("/users", headers={"X-API-Key": admin_key}, json=body).status_code == 201
    again = client.post("/users", headers={"X-API-Key": admin_key}, json=body)
    assert again.status_code == 400


def test_member_cannot_create_or_list_users(client, make_user):
    member = make_user()
    listed = client.get("/users", headers={"X-API-Key": member.key})
    assert listed.status_code == 403
    created = client.post(
        "/users",
        headers={"X-API-Key": member.key},
        json={
            "username": "x",
            "display_name": "X",
            "email": "x@example.com",
            "password": "pw1pw1",
        },
    )
    assert created.status_code == 403


def test_api_key_create_and_revoke(client, admin_key, make_user):
    member = make_user()

    # The member mints a key for themselves (self-or-admin allows self).
    created = client.post(
        f"/users/{member.id}/api-keys",
        headers={"X-API-Key": member.key},
        json={"name": "laptop"},
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    raw = payload["api_key"]
    key_id = payload["id"]

    # The new key authenticates.
    assert client.get("/auth/me", headers={"X-API-Key": raw}).status_code == 200

    # Revoke it; it should stop working.
    revoked = client.post(
        f"/users/{member.id}/api-keys/{key_id}/revoke", headers={"X-API-Key": member.key}
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert client.get("/auth/me", headers={"X-API-Key": raw}).status_code == 401


def test_member_cannot_manage_others_api_keys(client, admin_key, make_user):
    a = make_user()
    b = make_user()
    # b tries to list a's keys -> 403 (not self, not admin).
    r = client.get(f"/users/{a.id}/api-keys", headers={"X-API-Key": b.key})
    assert r.status_code == 403


def test_admin_cannot_delete_self(client, admin_key, admin_id):
    r = client.delete(f"/users/{admin_id}", headers={"X-API-Key": admin_key})
    assert r.status_code == 400


def test_admin_can_create_resolver_bot(client, admin_key):
    """The resolver-bot endpoint provisions a flagged member + a usable key in one
    call. The returned key authenticates and the user shows is_resolver_bot=True."""
    r = client.post(
        "/users/resolver-bot",
        headers={"X-API-Key": admin_key},
        json={"username": "botA", "display_name": "Bot A"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["username"] == "botA"
    assert body["api_key"].startswith("sk_")

    # The minted key authenticates.
    me = client.get("/auth/me", headers={"X-API-Key": body["api_key"]})
    assert me.status_code == 200
    assert me.json()["id"] == body["user_id"]

    # The user is flagged as a resolver bot in the admin listing.
    listed = client.get("/users", headers={"X-API-Key": admin_key}).json()
    bot = next(u for u in listed if u["id"] == body["user_id"])
    assert bot["is_resolver_bot"] is True
    assert bot["role"] == "member"


def test_resolver_bot_duplicate_username_rejected(client, admin_key):
    body = {"username": "botDupe"}
    assert client.post(
        "/users/resolver-bot", headers={"X-API-Key": admin_key}, json=body
    ).status_code == 201
    again = client.post(
        "/users/resolver-bot", headers={"X-API-Key": admin_key}, json=body
    )
    assert again.status_code == 400


def test_member_cannot_create_resolver_bot(client, make_user):
    member = make_user()
    r = client.post(
        "/users/resolver-bot",
        headers={"X-API-Key": member.key},
        json={"username": "botX"},
    )
    assert r.status_code == 403


# --- API key scopes ----------------------------------------------------------
# Scopes are admin-granted capabilities carried by a key (see control_tags
# .SCOPE_TAG_PREFIXES). The `cli` scope is what lets the stingray CLI set repo:
# tags without its owner being an admin.

def test_admin_can_grant_cli_scope(client, admin_key, make_user):
    member = make_user()
    created = client.post(
        f"/users/{member.id}/api-keys",
        headers={"X-API-Key": admin_key},
        json={"name": "laptop-cli", "scopes": ["cli"]},
    )
    assert created.status_code == 201, created.text
    assert created.json()["scopes"] == ["cli"]

    listed = client.get(f"/users/{member.id}/api-keys", headers={"X-API-Key": admin_key})
    entry = next(k for k in listed.json() if k["id"] == created.json()["id"])
    assert entry["scopes"] == ["cli"]


def test_member_cannot_self_grant_scope(client, make_user):
    """The escalation this gate exists to stop: a member may mint their own keys,
    so self-granting `cli` would let them aim the resolver at any repo under
    PROJECTS_ROOT and read its source back in a ticket they own."""
    member = make_user()
    r = client.post(
        f"/users/{member.id}/api-keys",
        headers={"X-API-Key": member.key},
        json={"name": "sneaky", "scopes": ["cli"]},
    )
    assert r.status_code == 403, r.text


def test_unknown_scope_rejected(client, admin_key, make_user):
    member = make_user()
    r = client.post(
        f"/users/{member.id}/api-keys",
        headers={"X-API-Key": admin_key},
        json={"name": "bad", "scopes": ["root"]},
    )
    assert r.status_code == 422, r.text


def test_key_without_scopes_defaults_empty(client, make_user):
    """Regression: existing callers pass no `scopes` and must be unaffected."""
    member = make_user()
    created = client.post(
        f"/users/{member.id}/api-keys",
        headers={"X-API-Key": member.key},
        json={"name": "plain"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["scopes"] == []
