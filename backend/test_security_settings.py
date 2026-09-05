"""Security settings: admin-managed settings that affect the app's security
posture (webhook SSRF exemptions, insecure-webhooks/dispatcher-pause toggles,
the lease TTL policy window, the per-user webhook cap).

Unlike resolver settings, this panel is gated behind `require_recent_admin` —
admin role AND a session cookie minted within the reauth window — not just
`require_admin`. That means it's reachable only via a fresh cookie login, not
an API key (however long-lived or admin-owned), so most tests here log in
through `new_client()` rather than using the shared `admin_key` fixture.
"""
import pytest

import auth
from database import SessionLocal
from models import SecuritySettings

H = lambda key: {"X-API-Key": key}  # noqa: E731


@pytest.fixture(autouse=True)
def _reset_security_settings():
    """This suite shares one DB and one global settings row (see conftest's
    docstring on accumulated state), but other suites — test_claims.py in
    particular — depend on the *default* lease-TTL window. Reset the row to
    defaults after every test here so a rejected-on-purpose write (or a
    persisted one, like the partial-merge tests) never leaks into unrelated
    tests."""
    yield
    db = SessionLocal()
    try:
        row = db.query(SecuritySettings).filter(SecuritySettings.id == 1).one_or_none()
        if row is not None:
            row.settings = {}
            db.commit()
    finally:
        db.close()


def _login(c, username: str, password: str):
    r = c.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def _admin_client(new_client):
    """A fresh cookie-authenticated session as the seeded admin."""
    return _login(new_client(), "admin", "admin")


def test_get_defaults(new_client):
    c = _admin_client(new_client)
    r = c.get("/security-settings")
    assert r.status_code == 200, r.text
    settings = r.json()["settings"]
    assert settings["webhook_allowed_hosts"] == []
    assert settings["allow_insecure_webhooks"] is False
    assert settings["dispatcher_paused"] is False
    assert settings["max_webhooks_per_user"] == 20


def test_put_persists_and_get_reflects(new_client, admin_id):
    c = _admin_client(new_client)
    r = c.put("/security-settings", json={
        "webhook_allowed_hosts": ["sink.internal.example"],
        "max_webhooks_per_user": 5,
    })
    assert r.status_code == 200, r.text
    settings = r.json()["settings"]
    assert settings["webhook_allowed_hosts"] == ["sink.internal.example"]
    assert settings["max_webhooks_per_user"] == 5
    assert r.json()["updated_by"] == admin_id

    g = c.get("/security-settings").json()["settings"]
    assert g["webhook_allowed_hosts"] == ["sink.internal.example"]
    assert g["max_webhooks_per_user"] == 5
    assert g["allow_insecure_webhooks"] is False  # untouched field keeps its default


def test_partial_update_merges(new_client):
    c = _admin_client(new_client)
    c.put("/security-settings", json={"dispatcher_paused": True})
    c.put("/security-settings", json={"allow_insecure_webhooks": True})
    g = c.get("/security-settings").json()["settings"]
    assert g["dispatcher_paused"] is True  # not clobbered by the second partial PUT
    assert g["allow_insecure_webhooks"] is True


def test_lease_window_rejects_below_hard_floor(new_client):
    c = _admin_client(new_client)
    r = c.put("/security-settings", json={"min_lease_ttl": 1})  # hard floor is 5
    assert r.status_code == 422, r.text


def test_lease_window_rejects_min_above_max(new_client):
    c = _admin_client(new_client)
    r = c.put("/security-settings", json={"min_lease_ttl": 100, "max_lease_ttl": 50})
    assert r.status_code == 422, r.text


def test_lease_window_rejects_default_outside_band(new_client):
    c = _admin_client(new_client)
    r = c.put("/security-settings", json={
        "min_lease_ttl": 10, "max_lease_ttl": 20, "default_lease_ttl": 30,
    })
    assert r.status_code == 422, r.text


def test_lease_window_partial_update_checked_against_merged_state(new_client):
    """A PUT that only sends one lease field is validated against what's
    already stored, not in isolation — sending max_lease_ttl below an
    already-stored min_lease_ttl must fail even though the payload alone
    looks fine."""
    c = _admin_client(new_client)
    ok = c.put("/security-settings", json={"min_lease_ttl": 100})
    assert ok.status_code == 200, ok.text
    bad = c.put("/security-settings", json={"max_lease_ttl": 50})
    assert bad.status_code == 422, bad.text


def test_unknown_field_rejected(new_client):
    c = _admin_client(new_client)
    r = c.put("/security-settings", json={"not_a_real_field": 1})
    assert r.status_code == 422, r.text


def test_non_admin_member_gets_403(new_client, make_user):
    member = make_user()
    c = _login(new_client(), member.username, member.password)
    r = c.get("/security-settings")
    assert r.status_code == 403, r.text
    r = c.put("/security-settings", json={"dispatcher_paused": True})
    assert r.status_code == 403, r.text


def test_admin_api_key_gets_reauth_required_not_a_bare_401(client, admin_key):
    """An API key can never satisfy require_recent_admin, however admin or
    long-lived — session_issued_at is only ever set on the cookie path. This
    is by design: the panel is a browser/UI-only surface."""
    r = client.get("/security-settings", headers=H(admin_key))
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "reauth_required"


def test_stale_admin_session_gets_reauth_required(new_client, monkeypatch):
    """A cookie session older than the reauth window is rejected the same way
    an API key is, even though the role check passes."""
    c = _admin_client(new_client)
    # Simulate time passing well past the reauth window without waiting for it.
    monkeypatch.setattr(auth, "REAUTH_WINDOW_SECONDS", -1)
    r = c.get("/security-settings")
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "reauth_required"


def test_fresh_relogin_restores_access_after_stale(new_client, monkeypatch):
    c = new_client()
    _login(c, "admin", "admin")
    monkeypatch.setattr(auth, "REAUTH_WINDOW_SECONDS", -1)
    assert c.get("/security-settings").status_code == 401
    # Fresh login re-mints the cookie with a new timestamp — even under the
    # same (still-tiny) window, "just logged in" must pass while "logged in a
    # moment ago" does not, so restore a real window before re-checking.
    monkeypatch.setattr(auth, "REAUTH_WINDOW_SECONDS", 15 * 60)
    _login(c, "admin", "admin")
    assert c.get("/security-settings").status_code == 200
