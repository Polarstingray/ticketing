"""Shared slowapi rate limiter.

Defined in its own module so both ``main.py`` (which registers the limiter and
the 429 handler on the app) and the routers (which decorate endpoints with
``@limiter.limit(...)``) can import it without a circular import on ``main``.

Storage is in-memory, which is correct for the current single-uvicorn-worker
deployment. If the app is ever scaled to multiple workers/replicas, pass a
``storage_uri="redis://..."`` here so the per-IP counters are shared.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

# ``get_remote_address`` uses ``request.client.host``, which reflects the real
# client only because uvicorn runs with ``--proxy-headers`` (see Dockerfile).
limiter = Limiter(key_func=get_remote_address, default_limits=[])
