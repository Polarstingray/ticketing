"""Read-only mode (READ_ONLY=true) — the guard the public Fly demo runs under.

The guard runs in ASGI middleware, ahead of routing, so it blocks a request
before FastAPI ever validates its body or resolves the caller — a test can hit
a write route with any body (even none) and the assertion is purely on the
response, not on what the route would otherwise have done with it.
"""
import demo_config
from read_only_guard import MESSAGE


def _auth(key):
    return {"X-API-Key": key}


def enable_read_only(monkeypatch, *, show_credentials=False):
    monkeypatch.setenv("READ_ONLY", "true")
    monkeypatch.setenv("SHOW_DEMO_CREDENTIALS", "true" if show_credentials else "false")
    demo_config.load.cache_clear()


def disable_read_only(monkeypatch):
    monkeypatch.delenv("READ_ONLY", raising=False)
    monkeypatch.delenv("SHOW_DEMO_CREDENTIALS", raising=False)
    demo_config.load.cache_clear()


# --- /app-config --------------------------------------------------------------

def test_app_config_defaults_to_off_and_no_credentials(client, monkeypatch):
    disable_read_only(monkeypatch)
    r = client.get("/app-config")
    assert r.status_code == 200
    assert r.json() == {"read_only": False, "demo_username": None, "demo_password": None}


def test_app_config_never_leaks_a_real_password_when_not_shown(client, monkeypatch):
    """READ_ONLY alone must never start publishing whatever ADMIN_PASSWORD a
    real self-hosted deployment happens to have set."""
    monkeypatch.setenv("READ_ONLY", "true")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-real-deployments-actual-password")
    monkeypatch.delenv("SHOW_DEMO_CREDENTIALS", raising=False)
    demo_config.load.cache_clear()
    try:
        r = client.get("/app-config")
        assert r.json()["read_only"] is True
        assert r.json()["demo_username"] is None
        assert r.json()["demo_password"] is None
        assert "a-real-deployments-actual-password" not in r.text
    finally:
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        demo_config.load.cache_clear()


def test_app_config_shows_credentials_only_when_opted_in(client, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "demopass123")
    enable_read_only(monkeypatch, show_credentials=True)
    try:
        r = client.get("/app-config")
        assert r.json() == {
            "read_only": True, "demo_username": "admin", "demo_password": "demopass123",
        }
    finally:
        monkeypatch.delenv("ADMIN_USERNAME", raising=False)
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        disable_read_only(monkeypatch)


# --- writes blocked, one per router ------------------------------------------

def test_writes_succeed_when_not_read_only(client, admin_key, monkeypatch):
    """The off-by-default smoke test: read-only mode must not be permanently
    on for every other deployment just because this module got imported."""
    disable_read_only(monkeypatch)
    r = client.post("/tickets", json={"type": "task", "title": "Off"}, headers=_auth(admin_key))
    assert r.status_code == 201, r.text


def test_ticket_writes_are_blocked(client, admin_key, monkeypatch):
    disable_read_only(monkeypatch)
    t = client.post("/tickets", json={"type": "task", "title": "Before lock"},
                    headers=_auth(admin_key)).json()
    enable_read_only(monkeypatch)
    try:
        for method, path, body in [
            ("POST", "/tickets", {"type": "task", "title": "New"}),
            ("PATCH", f"/tickets/{t['id']}", {"title": "Edited"}),
            ("DELETE", f"/tickets/{t['id']}", None),
        ]:
            r = client.request(method, path, json=body, headers=_auth(admin_key))
            assert r.status_code == 403, (method, path, r.text)
            assert r.json()["detail"] == MESSAGE
    finally:
        disable_read_only(monkeypatch)


def test_comment_writes_are_blocked(client, admin_key, monkeypatch):
    disable_read_only(monkeypatch)
    t = client.post("/tickets", json={"type": "task", "title": "Commentable"},
                    headers=_auth(admin_key)).json()
    enable_read_only(monkeypatch)
    try:
        r = client.post(f"/tickets/{t['id']}/comments", json={"body": "hi"},
                        headers=_auth(admin_key))
        assert r.status_code == 403
        assert r.json()["detail"] == MESSAGE
    finally:
        disable_read_only(monkeypatch)


def test_user_writes_are_blocked(client, admin_key, monkeypatch):
    """Includes the one that matters most: a visitor changing the shared
    admin's own password would lock out every other visitor until reboot."""
    enable_read_only(monkeypatch)
    try:
        r = client.post("/users", json={
            "username": "new", "password": "new12345", "email": "n@example.com",
            "role": "member",
        }, headers=_auth(admin_key))
        assert r.status_code == 403
        assert r.json()["detail"] == MESSAGE
    finally:
        disable_read_only(monkeypatch)


def test_resolver_settings_write_is_blocked(client, admin_key, monkeypatch):
    enable_read_only(monkeypatch)
    try:
        r = client.put("/resolver-settings", json={}, headers=_auth(admin_key))
        assert r.status_code == 403
        assert r.json()["detail"] == MESSAGE
    finally:
        disable_read_only(monkeypatch)


def test_webhook_write_is_blocked(client, admin_key, monkeypatch):
    enable_read_only(monkeypatch)
    try:
        r = client.post("/webhooks", json={"name": "ci", "url": "https://hooks.example.com/x"},
                        headers=_auth(admin_key))
        assert r.status_code == 403
        assert r.json()["detail"] == MESSAGE
    finally:
        disable_read_only(monkeypatch)


def test_saved_view_write_is_blocked(client, admin_key, monkeypatch):
    enable_read_only(monkeypatch)
    try:
        r = client.post("/saved-views", json={"name": "x", "filters": {}},
                        headers=_auth(admin_key))
        assert r.status_code == 403
        assert r.json()["detail"] == MESSAGE
    finally:
        disable_read_only(monkeypatch)


def test_preferences_write_is_blocked(client, admin_key, monkeypatch):
    enable_read_only(monkeypatch)
    try:
        r = client.put("/preferences", json={}, headers=_auth(admin_key))
        assert r.status_code == 403
        assert r.json()["detail"] == MESSAGE
    finally:
        disable_read_only(monkeypatch)


def test_notification_write_is_blocked(client, admin_key, monkeypatch):
    enable_read_only(monkeypatch)
    try:
        r = client.post("/notifications/read_all", headers=_auth(admin_key))
        assert r.status_code == 403
        assert r.json()["detail"] == MESSAGE
    finally:
        disable_read_only(monkeypatch)


# --- what stays open: signing in, and the chat assistant's own persistence --

def test_login_and_logout_still_work(client, make_user, monkeypatch):
    user = make_user()
    enable_read_only(monkeypatch)
    try:
        r = client.post("/auth/login", json={"username": user.username, "password": "member123"})
        assert r.status_code == 200, r.text
        r = client.post("/auth/logout")
        assert r.status_code == 200, r.text
    finally:
        disable_read_only(monkeypatch)


def test_chat_conversations_still_work(client, make_user, monkeypatch):
    """Asking a question persists a ChatMessage row, but that's chat history,
    not ticket data — and it's the feature being demoed."""
    from chat import config as chat_config
    from routers import chat as chat_router

    user = make_user()
    for key, value in {
        "CHAT_API_URL": "https://provider.example/v1/chat/completions",
        "CHAT_API_KEY": "sk-test-not-a-real-key",
        "CHAT_API_MODEL": "test-model",
    }.items():
        monkeypatch.setenv(key, value)
    chat_config.load.cache_clear()

    def fake_stream(cfg, system, messages, result, *, tools=None):
        result.text = "Hello."
        result.model = "test-model"
        yield "Hello."

    monkeypatch.setattr(chat_router.provider, "stream", fake_stream)
    enable_read_only(monkeypatch)
    try:
        r = client.post("/chat/conversations", json={}, headers=_auth(user.key))
        assert r.status_code == 201, r.text
        convo_id = r.json()["id"]

        r = client.post(f"/chat/conversations/{convo_id}/messages",
                        json={"content": "hi"}, headers=_auth(user.key))
        assert r.status_code == 200, r.text
        assert "event: done" in r.text
    finally:
        chat_config.load.cache_clear()
        disable_read_only(monkeypatch)


def test_a_confirmed_proposal_still_hits_the_block(client, make_user, monkeypatch):
    """propose_action itself is chat-scoped and exempt; the write it proposes
    is not. This is what makes 'let it fail naturally' true: the frontend's
    Confirm button needs no read-only awareness of its own."""
    user = make_user()
    enable_read_only(monkeypatch)
    try:
        r = client.post("/tickets", json={"type": "task", "title": "Proposed by the assistant"},
                        headers=_auth(user.key))
        assert r.status_code == 403
        assert r.json()["detail"] == MESSAGE
    finally:
        disable_read_only(monkeypatch)
