"""Startup safety checks.

Refuse to boot with insecure defaults once the app is treated as a real
deployment, while staying out of the way for local development. "Production"
is inferred from ``APP_ENV`` (``prod``/``production``/``staging``) or from
``COOKIE_SECURE=true`` (you only mark the cookie Secure when serving over
HTTPS, i.e. a real deployment).

In production an unset or placeholder ``SESSION_SECRET`` is fatal — a key that
is public (or that changes on every restart) cannot secure session cookies. A
weak ``ADMIN_PASSWORD`` is only a warning, since it just seeds the first admin
and can be changed after.
"""
import logging
import os

from auth import COOKIE_SECURE, SESSION_SECRET, SESSION_SECRET_IS_EPHEMERAL

log = logging.getLogger("stingray.startup")

# Well-known placeholder secrets from the compose file / docs / older releases.
# Booting with any of these in production means the cookie-signing key is public.
# (An *unset* SESSION_SECRET is caught separately via SESSION_SECRET_IS_EPHEMERAL:
# auth.py mints a random per-process key rather than a knowable default.)
WEAK_SESSION_SECRETS = {
    "",
    "dev-insecure-change-me",
    "please-change-me",
    "please-change-me-to-a-long-random-string",
    "change-me",
    "changeme",
}

# Obvious placeholder admin passwords from the docs/compose defaults.
WEAK_ADMIN_PASSWORDS = {"", "changeme", "changeme-please", "admin", "password", "admin123"}


def is_production() -> bool:
    """Treat the app as production when APP_ENV says so, or cookies are Secure."""
    app_env = os.environ.get("APP_ENV", "dev").strip().lower()
    if app_env in ("prod", "production", "staging"):
        return True
    return COOKIE_SECURE


def check_startup_security() -> None:
    """Validate secrets at boot. Raises RuntimeError in production on a default
    SESSION_SECRET; otherwise logs warnings. Safe/quiet for local dev."""
    weak_secret = SESSION_SECRET_IS_EPHEMERAL or SESSION_SECRET in WEAK_SESSION_SECRETS

    if not is_production():
        if SESSION_SECRET_IS_EPHEMERAL:
            log.warning(
                "SESSION_SECRET is unset; signing sessions with a random key "
                "generated for this process, so every restart logs everyone out. "
                "Set a real secret (e.g. `python -c \"import secrets; "
                "print(secrets.token_urlsafe(48))\"`) and APP_ENV=production "
                "before deploying."
            )
        elif weak_secret:
            log.warning(
                "SESSION_SECRET is a default/dev value. That's fine for local "
                "development, but set a real secret (e.g. `python -c \"import "
                "secrets; print(secrets.token_urlsafe(48))\"`) and APP_ENV=production "
                "before deploying."
            )
        return

    if weak_secret:
        raise RuntimeError(
            "Refusing to start: SESSION_SECRET is unset or a well-known default "
            "while running in production (APP_ENV=production or COOKIE_SECURE=true). "
            "Set SESSION_SECRET to a long random value, e.g. "
            "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`."
        )

    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if admin_password.strip().lower() in WEAK_ADMIN_PASSWORDS:
        log.warning(
            "ADMIN_PASSWORD looks like a weak/default value. If this is a fresh "
            "deployment the seeded admin will use it — change it immediately."
        )
