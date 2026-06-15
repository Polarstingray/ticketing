"""Stingray Tickets — FastAPI application entrypoint."""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from database import Base, SessionLocal, engine
from migrations import run_migrations
from ratelimit import limiter
from routers import auth as auth_router
from routers import comments as comments_router
from routers import notifications as notifications_router
from routers import preferences as preferences_router
from routers import tickets as tickets_router
from routers import users as users_router
from seed import seed_admin, seed_resolver_bot
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
    finally:
        db.close()
    yield


app = FastAPI(title="Stingray Tickets", version="1.0.0", lifespan=lifespan)

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
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(tickets_router.router)
app.include_router(comments_router.router)
app.include_router(notifications_router.router)
app.include_router(preferences_router.router)
app.include_router(users_router.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
