"""External agent identity: the `agent` key scope and the agent registry (#56).

A third-party worker must be able to authenticate as itself without being listed
in ``RESOLVER_BOT_USER_ID`` or promoted to admin. These tests pin the two halves
of that: what an ``agent``-scoped key may tag (a deliberately small slice — the
routing tags, never the aiming ones), and who may register in / read the agent
registry. Shares the one suite database, so assertions check membership by id.
"""
import uuid

H = lambda key: {"X-API-Key": key}  # noqa: E731


def _create(client, key, **overrides):
    body = {"type": "task", "title": "A ticket"}
    body.update(overrides)
    return client.post("/tickets", json=body, headers=H(key))


def _make_bot(client, admin_key, name):
    """Provision a resolver bot via the admin endpoint; returns (user_id, key)."""
    r = client.post(
        "/users/resolver-bot",
        json={"username": name, "display_name": name},
        headers=H(admin_key),
    )
    assert r.status_code == 201, r.text
    return r.json()["user_id"], r.json()["api_key"]


# --- Tags: `agent`-scoped API keys -------------------------------------------

def test_agent_scoped_key_can_set_routing_tags(client, make_user, scoped_key):
    """`parent:` and `review-by:` are the agent's own bookkeeping — how its work
    relates to other work — so the scope confers them."""
    member = make_user()
    key = scoped_key(member.id, "agent")
    r = _create(client, key, tags=["parent:7", "review-by:9", "backend"])
    assert r.status_code == 201, r.text
    assert set(r.json()["tags"]) == {"parent:7", "review-by:9", "backend"}


def test_agent_scoped_key_cannot_aim_the_resolver(client, make_user, scoped_key):
    """The `agent` scope is narrower than `cli`, not a superset: `repo:`/`rev:`/
    `branch:` point *our* automation at code of the caller's choosing, and the
    workflow/safety tags drive its state machine. Neither is the agent's to set."""
    member = make_user()
    key = scoped_key(member.id, "agent")
    for bad in ("repo:app", "rev:" + "a" * 40, "branch:main",
                "claude:planning", "dangerous", "fix", "delegate"):
        r = _create(client, key, tags=[bad])
        assert r.status_code == 422, f"{bad}: {r.text}"


def test_cli_scope_still_excludes_the_routing_tags(client, make_user, scoped_key):
    """Regression on the other direction: adding `agent` must not widen `cli`."""
    member = make_user()
    key = scoped_key(member.id, "cli")
    for bad in ("parent:7", "review-by:9"):
        r = _create(client, key, tags=[bad])
        assert r.status_code == 422, f"{bad}: {r.text}"


def test_unscoped_key_still_cannot_set_routing_tags(client, make_user):
    member = make_user()
    for bad in ("parent:7", "review-by:9"):
        r = _create(client, member.key, tags=[bad])
        assert r.status_code == 422, f"{bad}: {r.text}"


def test_agent_scope_does_not_leak_to_cookie_session(client, make_user, scoped_key):
    """The scope rides the key, so logging in as the same user must not inherit
    it — revoking the key has to revoke the capability."""
    member = make_user()
    scoped_key(member.id, "agent")
    login = client.post("/auth/login",
                        json={"username": member.username, "password": member.password})
    assert login.status_code == 200, login.text
    r = client.post("/tickets", json={"type": "task", "title": "x", "tags": ["parent:7"]})
    assert r.status_code == 422, r.text
    client.cookies.clear()


def test_agent_is_a_grantable_scope(client, admin_key, make_user):
    """`agent` has to be in ALL_SCOPES or the key-minting schema rejects it."""
    member = make_user()
    r = client.post(
        f"/users/{member.id}/api-keys",
        json={"name": "external", "scopes": ["agent"]},
        headers=H(admin_key),
    )
    assert r.status_code == 201, r.text
    assert r.json()["scopes"] == ["agent"]


def test_only_an_admin_may_grant_the_agent_scope(client, make_user):
    """Members mint their own keys, so self-service scoping would make the whole
    boundary decorative."""
    member = make_user()
    r = client.post(
        f"/users/{member.id}/api-keys",
        json={"name": "self-granted", "scopes": ["agent"]},
        headers=H(member.key),
    )
    assert r.status_code == 403, r.text


# --- The agent registry ------------------------------------------------------

def test_agent_heartbeat_registers_and_upserts(client, admin_key, make_user, scoped_key):
    member = make_user()
    key = scoped_key(member.id, "agent")
    r = client.post(
        "/agents/heartbeat",
        json={"label": "prod-us-east", "name": "triage", "agent": "custom",
              "model": "gpt-x", "effective_config": {"poll_seconds": 30}},
        headers=H(key),
    )
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry["user_id"] == member.id
    assert entry["name"] == "triage" and entry["is_resolver_bot"] is False
    assert entry["last_seen_at"] is not None

    # A second heartbeat updates the same row rather than appending one.
    first_seen = entry["last_seen_at"]
    r = client.post("/agents/heartbeat", json={"name": "triage", "model": "gpt-y"},
                    headers=H(key))
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "gpt-y"

    roster = client.get("/agents", headers=H(admin_key)).json()
    mine = [e for e in roster if e["user_id"] == member.id]
    assert len(mine) == 1 and mine[0]["model"] == "gpt-y"
    assert mine[0]["last_seen_at"] >= first_seen


def test_unscoped_key_cannot_heartbeat(client, make_user, scoped_key):
    """Neither an ordinary key nor a `cli` key is an agent credential."""
    member = make_user()
    for key in (member.key, scoped_key(member.id, "cli")):
        r = client.post("/agents/heartbeat", json={"name": "sneaky"}, headers=H(key))
        assert r.status_code == 403, r.text


def test_agent_heartbeat_rejects_unknown_keys(client, make_user, scoped_key):
    """`extra="forbid"`: a stray top-level key (a leaked secret, say) is a 422,
    not silently stored."""
    member = make_user()
    key = scoped_key(member.id, "agent")
    r = client.post("/agents/heartbeat",
                    json={"name": "x", "api_key": "sk_leak"}, headers=H(key))
    assert r.status_code == 422, r.text


def test_resolver_bot_appears_in_the_agent_registry(client, admin_key):
    """The registry is agent-neutral: our own resolvers are in it too, flagged as
    such, and `POST /resolvers/heartbeat` writes the same row."""
    uid, key = _make_bot(client, admin_key, f"agentreg-bot-{uuid.uuid4().hex[:6]}")
    r = client.post("/resolvers/heartbeat",
                    json={"name": "resolverish", "agent": "claude", "model": "m1"},
                    headers=H(key))
    assert r.status_code == 200, r.text

    roster = client.get("/agents", headers=H(admin_key)).json()
    mine = [e for e in roster if e["user_id"] == uid]
    assert len(mine) == 1
    assert mine[0]["is_resolver_bot"] is True and mine[0]["model"] == "m1"


def test_resolver_bot_may_use_the_agent_alias(client, admin_key):
    """A resolver bot's identity is itself the credential, so it needs no scope."""
    uid, key = _make_bot(client, admin_key, f"alias-bot-{uuid.uuid4().hex[:6]}")
    r = client.post("/agents/heartbeat", json={"name": "aliased"}, headers=H(key))
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == uid


def test_resolver_heartbeat_still_bot_only(client, make_user, scoped_key):
    """The agent scope registers a worker; it does not make one of our resolvers.
    `/resolvers/heartbeat` keeps its stricter gate."""
    member = make_user()
    key = scoped_key(member.id, "agent")
    r = client.post("/resolvers/heartbeat", json={"name": "impostor"}, headers=H(key))
    assert r.status_code == 403, r.text


def test_external_agent_absent_from_the_resolver_roster(client, admin_key, make_user, scoped_key):
    """`GET /resolvers` is the settings roster and stays keyed on the bot flag —
    an external agent has no resolver settings to manage."""
    member = make_user()
    key = scoped_key(member.id, "agent")
    client.post("/agents/heartbeat", json={"name": "outsider"}, headers=H(key))
    roster = client.get("/resolvers", headers=H(admin_key)).json()
    assert not [e for e in roster if e["bot_user_id"] == member.id]


def test_listing_agents_is_admin_only(client, make_user, scoped_key):
    """Who is running automation against the tracker is operator information."""
    member = make_user()
    for key in (member.key, scoped_key(member.id, "agent")):
        assert client.get("/agents", headers=H(key)).status_code == 403
