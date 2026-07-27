"""Shared slowapi rate limiter.

Defined in its own module so both ``main.py`` (which registers the limiter and
the 429 handler on the app) and the routers (which decorate endpoints with
``@limiter.limit(...)``) can import it without a circular import on ``main``.

Storage defaults to in-memory, which is correct for the current single-uvicorn-
worker deployment. If the app is scaled to multiple workers/replicas, set
``RATELIMIT_STORAGE_URI`` (e.g. ``redis://redis:6379``) so the per-IP counters are
shared across processes — a config change, no code change required.
"""
import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# Empty/unset => slowapi's default in-memory storage (per-process).
_storage_uri = os.environ.get("RATELIMIT_STORAGE_URI", "").strip() or None

# ``get_remote_address`` uses ``request.client.host``, which reflects the real
# client only because uvicorn runs with ``--proxy-headers`` (see Dockerfile).
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri=_storage_uri,
)

# Per-IP budget for ``POST /auth/login``. The default blunts network brute force
# while leaving room for fat-fingered passwords. It is configurable because a
# single IP can legitimately carry many logins — everyone behind one office NAT,
# or an end-to-end suite that signs in as several users inside a minute (the
# Playwright run sets a wider budget in playwright.config.js).
LOGIN_RATE_LIMIT = os.environ.get("LOGIN_RATE_LIMIT", "").strip() or "5/minute;30/hour"
