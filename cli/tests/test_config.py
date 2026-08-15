"""Credential store: precedence, file mode, and profile round-trips."""
from __future__ import annotations

import os
import stat

import pytest

from stingray_cli.config import (
    ConfigError,
    config_path,
    delete_profile,
    list_profiles,
    load_profile,
    save_profile,
)


def test_round_trip(isolated_config):
    save_profile("local", {"url": "http://x", "api_key": "sk_abc", "bot_user_id": 2})
    profile = load_profile()
    assert profile.name == "local"
    assert profile.url == "http://x"
    assert profile.api_key == "sk_abc"
    assert profile.bot_user_id == 2


def test_file_is_written_0600(isolated_config):
    save_profile("local", {"url": "http://x", "api_key": "sk_abc"})
    mode = stat.S_IMODE(config_path().stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {mode:o}"


def test_existing_loose_file_is_tightened_on_write(isolated_config):
    """O_CREAT keeps an existing file's mode, so the write must pin it explicitly."""
    isolated_config.write_text("")
    os.chmod(isolated_config, 0o644)
    save_profile("local", {"url": "http://x", "api_key": "sk_abc"})
    assert stat.S_IMODE(config_path().stat().st_mode) == 0o600


def test_loose_mode_warns(isolated_config, capsys):
    save_profile("local", {"url": "http://x", "api_key": "sk_abc"})
    os.chmod(isolated_config, 0o644)
    load_profile()
    assert "mode 644" in capsys.readouterr().err


def test_env_beats_profile(isolated_config, monkeypatch):
    save_profile("local", {"url": "http://from-file", "api_key": "sk_file"})
    monkeypatch.setenv("STINGRAY_URL", "http://from-env")
    profile = load_profile()
    assert profile.url == "http://from-env"
    assert profile.api_key == "sk_file"  # only the set var is overridden


def test_flag_beats_env(isolated_config, monkeypatch):
    save_profile("local", {"url": "http://from-file", "api_key": "sk_file"})
    monkeypatch.setenv("STINGRAY_URL", "http://from-env")
    profile = load_profile(url="http://from-flag")
    assert profile.url == "http://from-flag"


def test_env_only_needs_no_file(isolated_config, monkeypatch):
    """CI has no config file; env vars alone must be enough."""
    monkeypatch.setenv("STINGRAY_URL", "http://ci")
    monkeypatch.setenv("STINGRAY_API_KEY", "sk_ci")
    profile = load_profile()
    assert (profile.url, profile.api_key) == ("http://ci", "sk_ci")


def test_missing_profile_names_the_fix(isolated_config):
    save_profile("local", {"url": "http://x", "api_key": "sk_abc"})
    with pytest.raises(ConfigError) as exc:
        load_profile("nope")
    assert "no profile named 'nope'" in str(exc.value)
    assert "stingray auth login" in str(exc.value)


def test_unauthenticated_message(isolated_config):
    with pytest.raises(ConfigError) as exc:
        load_profile()
    assert "stingray auth login" in str(exc.value)


def test_multiple_profiles_are_independent(isolated_config):
    save_profile("a", {"url": "http://a", "api_key": "sk_a"})
    save_profile("b", {"url": "http://b", "api_key": "sk_b"})
    assert load_profile("a").url == "http://a"
    assert load_profile("b").url == "http://b"
    # The first profile written stays the default.
    assert load_profile().name == "a"


def test_logout_leaves_other_profiles(isolated_config):
    save_profile("a", {"url": "http://a", "api_key": "sk_a"})
    save_profile("b", {"url": "http://b", "api_key": "sk_b"})
    assert delete_profile("a") is True
    profiles, default = list_profiles()
    assert set(profiles) == {"b"}
    assert default == "b"  # the default moved off the deleted profile
    assert delete_profile("a") is False


def test_describe_settings_survive_a_round_trip(isolated_config):
    save_profile("local", {
        "url": "http://x", "api_key": "sk_abc",
        "describe": {"agent": "opencode", "timeout": 90},
    })
    profile = load_profile()
    assert profile.describe == {"agent": "opencode", "timeout": 90}


def test_key_display_never_shows_the_whole_key(isolated_config):
    save_profile("local", {"url": "http://x", "api_key": "sk_secretsecretsecret"})
    display = load_profile().key_display
    assert "secretsecret" not in display
    assert display.startswith("sk_")


def test_web_url_strips_the_api_proxy_prefix(isolated_config):
    """The REST base often ends in /api (the frontend's proxy). A browser link
    built from that 404s, so the printed ticket URL must drop it."""
    save_profile("p", {"url": "http://localhost:3000/api", "api_key": "sk_a"})
    assert load_profile("p").web_url == "http://localhost:3000"


def test_web_url_left_alone_without_the_prefix(isolated_config):
    save_profile("p", {"url": "http://localhost:8000", "api_key": "sk_a"})
    assert load_profile("p").web_url == "http://localhost:8000"


def test_web_url_handles_a_trailing_slash(isolated_config):
    """`endswith("/api")` alone would miss `.../api/`; load_profile rstrips the
    URL first, so both forms normalize. Flagged by a review of the web_url diff,
    which couldn't see the rstrip."""
    save_profile("p", {"url": "http://localhost:3000/api/", "api_key": "sk_a"})
    profile = load_profile("p")
    assert profile.url == "http://localhost:3000/api"
    assert profile.web_url == "http://localhost:3000"
