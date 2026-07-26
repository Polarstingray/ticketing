"""Resolver-side settings overlay: server-managed tunables layer onto the
.env-derived config, secrets are never touched, and absent/unreachable settings
fall back to the .env values."""
from types import SimpleNamespace

import resolve_tickets as rt
import stingray


def test_empty_remote_leaves_cfg_untouched(fake_cfg):
    fake_cfg.max_attempts = 3
    rt._overlay_settings(fake_cfg, {})
    assert fake_cfg.max_attempts == 3


def test_none_values_are_ignored(fake_cfg):
    fake_cfg.max_attempts = 3
    rt._overlay_settings(fake_cfg, {"max_attempts": None, "verify_command": None})
    assert fake_cfg.max_attempts == 3
    assert fake_cfg.verify_command == ""


def test_values_win_when_present(fake_cfg):
    rt._overlay_settings(fake_cfg, {
        "max_attempts": 7,
        "verify_command": "pytest -q",
        "agent_fallback_models": ["a", "b"],
        "escalate_priorities": ["critical"],
        "repo_map": {"acme": "/srv/acme"},
        "allow_delegation": True,
        "escalate_to_user_id": 42,
    })
    assert fake_cfg.max_attempts == 7
    assert fake_cfg.verify_command == "pytest -q"
    assert fake_cfg.agent_fallback_models == ["a", "b"]
    assert fake_cfg.escalate_priorities == ["critical"]
    assert fake_cfg.repo_map == {"acme": "/srv/acme"}
    assert fake_cfg.allow_delegation is True
    assert fake_cfg.escalate_to_user_id == 42


def test_int_fields_coerced_from_strings(fake_cfg):
    # A JSON blob mucked by hand could carry stringy numbers; coerce, don't crash.
    rt._overlay_settings(fake_cfg, {"max_attempts": "5", "verify_timeout": "30"})
    assert fake_cfg.max_attempts == 5
    assert fake_cfg.verify_timeout == 30


def test_malformed_int_is_skipped(fake_cfg):
    fake_cfg.max_attempts = 3
    rt._overlay_settings(fake_cfg, {"max_attempts": "not-a-number"})
    assert fake_cfg.max_attempts == 3  # left at the .env value rather than crashing


def test_secrets_are_never_applied(fake_cfg):
    # Secret field names are not in the overlay whitelist, so even if a server
    # response somehow carried them they must not overwrite the .env values.
    fake_cfg.api_key = "sk_env_secret"
    fake_cfg.review_api_key = "sk_env_review"
    rt._overlay_settings(fake_cfg, {
        "api_key": "sk_injected",
        "review_api_key": "sk_injected",
        "stingray_url": "http://evil",
    })
    assert fake_cfg.api_key == "sk_env_secret"
    assert fake_cfg.review_api_key == "sk_env_review"


def test_identity_name_mapping():
    from config import _identity_name
    assert _identity_name(".env") == "default"
    assert _identity_name(".env.gemini") == "gemini"
    assert _identity_name(".env.open") == "open"
    # An absolute/relative path is reduced to its basename first.
    assert _identity_name("/srv/resolver/.env.open") == "open"


def test_effective_snapshot_excludes_secrets(fake_cfg):
    # A secret set on the config must never appear in the registry snapshot.
    fake_cfg.api_key = "sk_env_secret"
    fake_cfg.review_api_key = "sk_env_review"
    snap = rt._effective_snapshot(fake_cfg)
    assert "api_key" not in snap
    assert "review_api_key" not in snap
    assert "stingray_url" not in snap
    # ...but the non-secret tunables it does have are captured.
    assert snap["max_attempts"] == fake_cfg.max_attempts
    assert "verify_command" in snap


def test_heartbeat_posts_to_registry(monkeypatch):
    calls = {}

    class _Resp:
        def json(self):
            return {"bot_user_id": 3}

    client = stingray.StingrayClient.__new__(stingray.StingrayClient)

    def fake_request(method, path, json=None, **kw):
        calls["method"], calls["path"], calls["json"] = method, path, json
        return _Resp()

    monkeypatch.setattr(client, "_request", fake_request)
    out = stingray.StingrayClient.heartbeat(client, name="gemini", agent="opencode")
    assert calls["method"] == "POST" and calls["path"] == "/resolvers/heartbeat"
    assert calls["json"] == {"name": "gemini", "agent": "opencode"}
    assert out["bot_user_id"] == 3


def test_get_resolver_settings_passes_bot_id(monkeypatch):
    # The client method sends bot_user_id as a query param and returns the JSON.
    calls = {}

    class _Resp:
        def json(self):
            return {"settings": {"max_attempts": 4}}

    client = stingray.StingrayClient.__new__(stingray.StingrayClient)

    def fake_request(method, path, params=None, **kw):
        calls["method"], calls["path"], calls["params"] = method, path, params
        return _Resp()

    monkeypatch.setattr(client, "_request", fake_request)
    out = stingray.StingrayClient.get_resolver_settings(client, bot_user_id=2)
    assert calls == {"method": "GET", "path": "/resolver-settings", "params": {"bot_user_id": 2}}
    assert out["settings"]["max_attempts"] == 4
