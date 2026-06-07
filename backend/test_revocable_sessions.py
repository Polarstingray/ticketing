"""Tests for revocable sessions (ticket #14).

Verifies that logout, password change, and role change invalidate existing
session cookies, and that pre-change (versionless) tokens are rejected. Uses the
shared fixtures in conftest.py; `new_client` supplies fresh cookie jars to
simulate separate devices.
"""
import auth


def _login(client, username="admin", password="admin"):
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


def test_login_me_works(new_client):
    c = new_client()
    _login(c)
    assert c.get("/auth/me").status_code == 200


def test_logout_invalidates_reused_cookie(new_client):
    c = new_client()
    _login(c)
    cookie = c.cookies.get("session")
    assert c.get("/auth/me").status_code == 200

    c.post("/auth/logout")

    # Reuse the *same* old cookie value on a fresh client -> must be rejected.
    fresh = new_client()
    fresh.cookies.set("session", cookie)
    assert fresh.get("/auth/me").status_code == 401


def test_unauthenticated_logout_still_ok(new_client):
    c = new_client()
    assert c.post("/auth/logout").status_code == 200


def test_old_format_token_without_sv_rejected(new_client):
    # Mint a token in the pre-change format (no "sv").
    legacy = auth._serializer.dumps({"user_id": 1})
    c = new_client()
    c.cookies.set("session", legacy)
    assert c.get("/auth/me").status_code == 401


def test_password_change_logs_out_all_devices(client, admin_key, new_client, make_user):
    bob = make_user(username="bob", password="pw1pw1")

    # Bob logs in on two "devices".
    dev_a = new_client()
    _login(dev_a, "bob", "pw1pw1")
    dev_b = new_client()
    _login(dev_b, "bob", "pw1pw1")
    assert dev_a.get("/auth/me").status_code == 200
    assert dev_b.get("/auth/me").status_code == 200

    # Admin resets bob's password.
    client.patch(
        f"/users/{bob.id}", headers={"X-API-Key": admin_key}, json={"password": "pw2pw2"}
    )

    assert dev_a.get("/auth/me").status_code == 401
    assert dev_b.get("/auth/me").status_code == 401

    # Fresh login with the new password works.
    dev_c = new_client()
    _login(dev_c, "bob", "pw2pw2")
    assert dev_c.get("/auth/me").status_code == 200


def test_role_change_invalidates_session(client, admin_key, new_client, make_user):
    carol = make_user(username="carol", password="pw1pw1")

    dev = new_client()
    _login(dev, "carol", "pw1pw1")
    assert dev.get("/auth/me").status_code == 200

    client.patch(
        f"/users/{carol.id}", headers={"X-API-Key": admin_key}, json={"role": "admin"}
    )

    assert dev.get("/auth/me").status_code == 401


def test_api_key_auth_unaffected(client, admin_key):
    assert client.get("/auth/me", headers={"X-API-Key": admin_key}).status_code == 200
