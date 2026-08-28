"""Startup secret checks.

The suite runs with SESSION_SECRET unset (conftest never sets it), so these also
cover the "no configuration at all" path: auth mints a random per-process key
instead of falling back to a value baked into the source.
"""
import logging

import pytest

import auth
import startup


def test_unset_session_secret_is_random_not_a_baked_in_default():
    assert auth.SESSION_SECRET_IS_EPHEMERAL is True
    assert auth.SESSION_SECRET not in startup.WEAK_SESSION_SECRETS
    assert auth.SESSION_SECRET != "dev-insecure-change-me"
    assert len(auth.SESSION_SECRET) >= 32


def test_dev_boot_with_ephemeral_secret_warns_but_starts(monkeypatch, caplog):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setattr(startup, "COOKIE_SECURE", False)
    with caplog.at_level(logging.WARNING, logger="stingray.startup"):
        startup.check_startup_security()
    assert "SESSION_SECRET is unset" in caplog.text


def test_production_refuses_to_start_when_secret_is_unset(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        startup.check_startup_security()


@pytest.mark.parametrize("secret", sorted(startup.WEAK_SESSION_SECRETS))
def test_production_refuses_to_start_on_a_well_known_secret(monkeypatch, secret):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(startup, "SESSION_SECRET_IS_EPHEMERAL", False)
    monkeypatch.setattr(startup, "SESSION_SECRET", secret)
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        startup.check_startup_security()


def test_cookie_secure_alone_implies_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setattr(startup, "COOKIE_SECURE", True)
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        startup.check_startup_security()


def test_real_secret_in_production_boots(monkeypatch, caplog):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-genuinely-long-admin-password")
    monkeypatch.setattr(startup, "SESSION_SECRET_IS_EPHEMERAL", False)
    monkeypatch.setattr(startup, "SESSION_SECRET", "x" * 48)
    with caplog.at_level(logging.WARNING, logger="stingray.startup"):
        startup.check_startup_security()
    assert caplog.text == ""


def test_weak_admin_password_only_warns(monkeypatch, caplog):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_PASSWORD", "changeme")
    monkeypatch.setattr(startup, "SESSION_SECRET_IS_EPHEMERAL", False)
    monkeypatch.setattr(startup, "SESSION_SECRET", "x" * 48)
    with caplog.at_level(logging.WARNING, logger="stingray.startup"):
        startup.check_startup_security()
    assert "ADMIN_PASSWORD" in caplog.text
