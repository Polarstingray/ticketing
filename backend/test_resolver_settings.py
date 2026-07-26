"""Resolver settings: admin-managed, non-secret resolver tunables.

Verifies the CRUD + admin gating + the secret-safety guarantees (secrets are
never accepted on write, never returned as values on read). Shares the one
suite database, so assertions tolerate accumulated rows.
"""

H = lambda key: {"X-API-Key": key}  # noqa: E731


def test_get_defaults_and_secret_descriptors(client, admin_key):
    r = client.get("/resolver-settings", headers=H(admin_key))
    assert r.status_code == 200, r.text
    body = r.json()
    # Defaults present when nothing is stored.
    assert body["settings"]["max_attempts"] == 3
    assert body["settings"]["escalate_priorities"] == ["high", "critical"]
    # Secrets are described but never valued.
    names = {s["name"] for s in body["secrets"]}
    assert "STINGRAY_API_KEY" in names
    for s in body["secrets"]:
        assert "value" not in s and "key" not in s
        assert s["managed_in"] == ".env"


def test_admin_put_persists_and_get_reflects(client, admin_key, admin_id):
    r = client.put(
        "/resolver-settings",
        json={"max_attempts": 5, "verify_command": "pytest -q",
              "agent_fallback_models": ["a", "b"], "allow_delegation": True},
        headers=H(admin_key),
    )
    assert r.status_code == 200, r.text
    settings = r.json()["settings"]
    assert settings["max_attempts"] == 5
    assert settings["verify_command"] == "pytest -q"
    assert settings["agent_fallback_models"] == ["a", "b"]
    assert settings["allow_delegation"] is True
    assert r.json()["updated_by"] == admin_id

    # A separate GET reflects the stored values (and untouched fields keep defaults).
    g = client.get("/resolver-settings", headers=H(admin_key)).json()
    assert g["settings"]["max_attempts"] == 5
    assert g["settings"]["max_tickets_per_sweep"] == 0  # never set -> default


def test_partial_update_merges(client, admin_key):
    client.put("/resolver-settings", json={"max_attempts": 7}, headers=H(admin_key))
    client.put("/resolver-settings", json={"default_repo": "acme"}, headers=H(admin_key))
    g = client.get("/resolver-settings", headers=H(admin_key)).json()["settings"]
    assert g["max_attempts"] == 7  # not clobbered by the second partial PUT
    assert g["default_repo"] == "acme"


def test_non_admin_cannot_write(client, make_user):
    member = make_user()
    r = client.put("/resolver-settings", json={"max_attempts": 9}, headers=H(member.key))
    assert r.status_code == 403


def test_non_admin_can_read(client, make_user):
    member = make_user()
    r = client.get("/resolver-settings", headers=H(member.key))
    assert r.status_code == 200


def test_put_rejects_secret_key(client, admin_key):
    # extra="forbid" means any secret (or unknown) key is a 422, never silently stored.
    for secret in ("stingray_api_key", "review_api_key", "critique_api_key"):
        r = client.put("/resolver-settings", json={secret: "sk_leak"}, headers=H(admin_key))
        assert r.status_code == 422, f"{secret} should be rejected"


def test_bot_scoped_overrides_global(client, admin_key):
    # Global default row.
    client.put("/resolver-settings", json={"max_attempts": 2}, headers=H(admin_key))
    # A bot-specific row overrides the global for that bot only.
    client.put("/resolver-settings?bot_user_id=42", json={"max_attempts": 8}, headers=H(admin_key))
    glob = client.get("/resolver-settings", headers=H(admin_key)).json()["settings"]
    bot = client.get("/resolver-settings?bot_user_id=42", headers=H(admin_key)).json()["settings"]
    assert glob["max_attempts"] == 2
    assert bot["max_attempts"] == 8


def test_bot_falls_back_to_global(client, admin_key):
    client.put("/resolver-settings", json={"quota_backoff_minutes": 15}, headers=H(admin_key))
    # A bot with no row of its own inherits the global value.
    bot = client.get("/resolver-settings?bot_user_id=9999", headers=H(admin_key)).json()["settings"]
    assert bot["quota_backoff_minutes"] == 15


# --- Resolver registry (the live manager) ------------------------------------

def _make_bot(client, admin_key, name):
    """Provision a resolver bot via the admin endpoint; returns (user_id, key)."""
    r = client.post(
        "/users/resolver-bot",
        json={"username": name, "display_name": name},
        headers=H(admin_key),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return body["user_id"], body["api_key"]


def test_heartbeat_upserts_and_roster_shows_live_fields(client, admin_key):
    uid, key = _make_bot(client, admin_key, f"gemini-bot-{__import__('uuid').uuid4().hex[:6]}")
    r = client.post(
        "/resolvers/heartbeat",
        json={"label": ".env.gemini", "name": "gemini", "agent": "opencode",
              "model": "google/gemini-2.5-flash",
              "effective_config": {"max_attempts": 4, "agent_model": "google/gemini-2.5-flash"}},
        headers=H(key),
    )
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry["bot_user_id"] == uid and entry["name"] == "gemini"
    assert entry["last_seen_at"] is not None

    roster = client.get("/resolvers", headers=H(admin_key)).json()
    mine = next(e for e in roster if e["bot_user_id"] == uid)
    assert mine["agent"] == "opencode"
    assert mine["model"] == "google/gemini-2.5-flash"
    assert mine["effective_config"]["max_attempts"] == 4


def test_heartbeat_updates_in_place(client, admin_key):
    uid, key = _make_bot(client, admin_key, f"open-bot-{__import__('uuid').uuid4().hex[:6]}")
    client.post("/resolvers/heartbeat", json={"name": "open", "model": "m1"}, headers=H(key))
    client.post("/resolvers/heartbeat", json={"name": "open", "model": "m2"}, headers=H(key))
    roster = client.get("/resolvers", headers=H(admin_key)).json()
    mine = [e for e in roster if e["bot_user_id"] == uid]
    assert len(mine) == 1 and mine[0]["model"] == "m2"  # upsert, not append


def test_non_bot_cannot_heartbeat(client, make_user):
    member = make_user()  # a normal member, not a resolver bot
    r = client.post("/resolvers/heartbeat", json={"name": "sneaky"}, headers=H(member.key))
    assert r.status_code == 403


def test_heartbeat_rejects_secret_key(client, admin_key):
    _, key = _make_bot(client, admin_key, f"claude-bot-{__import__('uuid').uuid4().hex[:6]}")
    r = client.post("/resolvers/heartbeat",
                    json={"name": "x", "stingray_api_key": "sk_leak"}, headers=H(key))
    assert r.status_code == 422


def test_roster_requires_admin(client, make_user):
    member = make_user()
    assert client.get("/resolvers", headers=H(member.key)).status_code == 403


def test_bot_without_heartbeat_appears_with_null_live_fields(client, admin_key):
    uid, _ = _make_bot(client, admin_key, f"idle-bot-{__import__('uuid').uuid4().hex[:6]}")
    roster = client.get("/resolvers", headers=H(admin_key)).json()
    mine = next(e for e in roster if e["bot_user_id"] == uid)
    assert mine["last_seen_at"] is None and mine["agent"] is None
    assert mine["has_settings"] is False


def test_has_settings_flips_after_override(client, admin_key):
    uid, _ = _make_bot(client, admin_key, f"cfg-bot-{__import__('uuid').uuid4().hex[:6]}")
    client.put(f"/resolver-settings?bot_user_id={uid}", json={"max_attempts": 9}, headers=H(admin_key))
    roster = client.get("/resolvers", headers=H(admin_key)).json()
    mine = next(e for e in roster if e["bot_user_id"] == uid)
    assert mine["has_settings"] is True
