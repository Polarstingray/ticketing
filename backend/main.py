"""Stingray Tickets — FastAPI application entrypoint."""
import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import demo_config
import dispatcher
from database import Base, SessionLocal, engine
from migrations import run_migrations
from ratelimit import limiter
from read_only_guard import read_only_guard
from routers import auth as auth_router
from routers import chat as chat_router
from routers import comments as comments_router
from routers import events as events_router
from routers import notifications as notifications_router
from routers import preferences as preferences_router
from routers import resolver_settings as resolver_settings_router
from routers import saved_views as saved_views_router
from routers import tickets as tickets_router
from routers import users as users_router
from routers import webhooks as webhooks_router
from seed import seed_admin, seed_digest_admin_key, seed_resolver_bot
from startup import check_startup_security


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast on insecure defaults before doing anything else (no-op in dev).
    check_startup_security()
    # Create tables, apply idempotent migrations, and seed the first admin.
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    db = SessionLocal()
    try:
        seed_admin(db)
        seed_resolver_bot(db)
        seed_digest_admin_key(db)
    finally:
        db.close()

    # The webhook dispatcher runs in-process. That is sound *because the Fly
    # demo is pinned to one machine*: two of these against one SQLite file would
    # double-deliver, since claiming a batch is not fenced against another
    # process. Scaling out means a real broker, and there is no demand for one.
    task = asyncio.create_task(dispatcher.run_dispatcher()) if dispatcher.enabled() else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            # Awaiting the cancellation is what makes shutdown orderly: it lets
            # the in-flight pass unwind and close its sessions before the
            # process exits.
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Stingray Tickets", version="1.2.0", lifespan=lifespan)

# Rate limiting (slowapi). Routers reach the limiter via app.state.limiter / the
# shared ratelimit module; RateLimitExceeded is rendered as HTTP 429 with a
# Retry-After header by slowapi's built-in handler.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — in dev the Vite server runs on a different origin; in prod nginx serves
# the SPA same-origin and proxies /api, so this mainly matters for development.
cors_origins = os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Read-only mode (the public demo). A no-op unless READ_ONLY=true; see
# read_only_guard.py for what's exempt and why.
app.middleware("http")(read_only_guard)

app.include_router(auth_router.router)
app.include_router(tickets_router.router)
app.include_router(chat_router.router)
app.include_router(comments_router.router)
app.include_router(events_router.router)
app.include_router(notifications_router.router)
app.include_router(preferences_router.router)
app.include_router(resolver_settings_router.router)
app.include_router(resolver_settings_router.registry_router)
app.include_router(resolver_settings_router.agents_router)
app.include_router(saved_views_router.router)
app.include_router(users_router.router)
app.include_router(webhooks_router.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/app-config", tags=["meta"])
def app_config():
    """Whether this deployment is read-only, and demo credentials if the
    operator opted into showing them. Unauthenticated: the Login page needs
    this before a session cookie exists."""
    return demo_config.load().public()
