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
