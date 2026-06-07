"""Stingray Tickets — FastAPI application entrypoint."""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import inspect, text

from auth import COOKIE_SECURE
from database import Base, SessionLocal, engine
from ratelimit import limiter
from routers import auth as auth_router
from routers import comments as comments_router
from routers import tickets as tickets_router
from routers import users as users_router
from seed import seed_admin


def _migrate_session_version():
    """Add the users.session_version column to pre-existing databases.

    ``create_all`` never alters tables that already exist, and there's no
    Alembic in this project, so an older ``stingray.db`` would be missing the
    column. This idempotent guard adds it when absent.
    """
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("users")}
    if "session_version" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables and seed the first admin on startup.
    Base.metadata.create_all(bind=engine)
    _migrate_session_version()
    db = SessionLocal()
    try:
        seed_admin(db)
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
    # Narrowed to the methods/headers the app actually uses (plus the implicit
    # OPTIONS preflight and Content-Type). X-API-Key carries programmatic auth.
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)


# Security headers. The backend serves JSON only, so a tight CSP is safe here;
# this is the dev safety net (Vite has no nginx) and guarantees API responses
# carry these headers regardless of any fronting proxy. nginx adds the
# document-level CSP for the built SPA in production.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    # HSTS only over HTTPS (prod). COOKIE_SECURE is the existing "served over
    # TLS" signal; sending HSTS on plain-HTTP dev would poison localhost.
    if COOKIE_SECURE:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
    return response


app.include_router(auth_router.router)
app.include_router(tickets_router.router)
app.include_router(comments_router.router)
app.include_router(users_router.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
