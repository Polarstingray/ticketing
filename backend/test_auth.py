"""Login / logout / current-user flows and credential checks.

The per-IP slowapi limiter is disabled suite-wide (conftest) and the in-memory
account/IP throttles are reset between tests, so these exercise the auth logic
itself rather than the rate limits.
"""


def test_login_success_sets_cookie(new_client):
    c = new_client()
    r = c.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200, r.text
    assert c.cookies.get("session")
    assert r.json()["username"] == "admin"


def test_login_wrong_password_401(new_client):
    c = new_client()
    r = c.post("/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    assert not c.cookies.get("session")


def test_login_unknown_user_401(new_client):
    c = new_client()
    r = c.post("/auth/login", json={"username": "nobody-here", "password": "whatever"})
    assert r.status_code == 401


def test_me_requires_auth(new_client):
    c = new_client()
    assert c.get("/auth/me").status_code == 401


def test_login_me_logout_cycle(new_client):
    c = new_client()
    c.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert c.get("/auth/me").status_code == 200
    assert c.post("/auth/logout").status_code == 200


def test_session_cookie_auth_works_for_me(new_client):
    c = new_client()
    c.post("/auth/login", json={"username": "admin", "password": "admin"})
    me = c.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["role"] == "admin"


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
