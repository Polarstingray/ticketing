"""Credential store: precedence, file mode, and profile round-trips."""
from __future__ import annotations

import os
import stat

import pytest

from stingray_cli import config
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


# --- wrong base URL ----------------------------------------------------------

def test_html_response_names_the_url_problem():
    """Pointing at the SPA root instead of /api returned a 200 of index.html,
    and decoding that raised "Expecting value: line 1 column 1" — which reads as
    a parse bug rather than a wrong URL."""
    import requests

    from stingray_client.api import NotJsonError, StingrayClient

    class FakeResp:
        status_code = 200
        url = "http://localhost:3000/auth/me"
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def raise_for_status(self):
            pass

    client = StingrayClient("http://localhost:3000", "sk_x")
    client.session = type("S", (), {"request": lambda *a, **kw: FakeResp()})()

    with pytest.raises(NotJsonError) as exc:
        client.whoami()
    message = str(exc.value)
    assert "text/html" in message
    assert "/api" in message, "must suggest the fix, not just report the symptom"
    assert isinstance(exc.value, Exception) and not isinstance(
        exc.value, requests.RequestException
    ), "must not be swallowed by the 'could not reach' handler"


def test_json_response_passes_through():
    from stingray_client.api import StingrayClient

    class FakeResp:
        status_code = 200
        url = "http://localhost:3000/api/auth/me"
        headers = {"Content-Type": "application/json"}

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": 1, "username": "admin"}

    client = StingrayClient("http://localhost:3000/api", "sk_x")
    client.session = type("S", (), {"request": lambda *a, **kw: FakeResp()})()
    assert client.whoami()["username"] == "admin"


def test_missing_content_type_is_allowed():
    """Regression: the first cut of this check treated a *missing* Content-Type
    as an error, which broke every response double that omits headers (and would
    reject 204s and proxies that don't set it). Absence proves nothing."""
    from stingray_client.api import StingrayClient

    class FakeResp:
        status_code = 200
        url = "http://h/api/auth/me"
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": 1, "username": "admin"}

    client = StingrayClient("http://h/api", "sk_x")
    client.session = type("S", (), {"request": lambda *a, **kw: FakeResp()})()
    assert client.whoami()["username"] == "admin"

def test_write_secure_propagates_the_real_error_on_a_failed_write(tmp_path, monkeypatch):
    """A write failure must surface as itself, not as EBADF from a second close.

    `os.fdopen` owns the descriptor once it succeeds, so closing the raw fd in
    an `except` around the write closes it twice. Best case that masks the real
    error; worst case the number has been recycled and an unrelated file is
    closed under another thread.
    """
    class DiskFull(Exception):
        pass

    real_fdopen = os.fdopen

    def fdopen_that_fails_to_write(fd, *args, **kwargs):
        handle = real_fdopen(fd, *args, **kwargs)
        def boom(_text):
            raise DiskFull("no space left on device")
        handle.write = boom
        return handle

    monkeypatch.setattr(os, "fdopen", fdopen_that_fails_to_write)
    with pytest.raises(DiskFull):
        config.write_secure(tmp_path / "x.toml", "hello")


def test_write_secure_closes_the_descriptor_if_fdopen_fails(tmp_path, monkeypatch):
    """The one path where the raw close is still ours to make."""
    closed = []
    real_close = os.close

    def failing_fdopen(fd, *args, **kwargs):
        raise OSError("cannot wrap")

    monkeypatch.setattr(os, "fdopen", failing_fdopen)
    monkeypatch.setattr(os, "close", lambda fd: (closed.append(fd), real_close(fd))[1])
    with pytest.raises(OSError):
        config.write_secure(tmp_path / "y.toml", "hello")
    assert len(closed) == 1
