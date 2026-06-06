"""Stingray Tickets — FastAPI application entrypoint."""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import inspect, text

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


def _migrate_archived():
    """Add the `tickets.archived` column to pre-existing databases.

    `create_all` only creates missing tables; it won't alter an existing
    `tickets` table, and there's no Alembic in this project. This is a small,
    idempotent migration that adds the column when it's absent.
    """
    with engine.begin() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(tickets)"))]
        # `cols` is empty if the table doesn't exist yet (create_all handles that).
        if cols and "archived" not in cols:
            conn.execute(
                text("ALTER TABLE tickets ADD COLUMN archived BOOLEAN NOT NULL DEFAULT 0")
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables and seed the first admin on startup.
    Base.metadata.create_all(bind=engine)
    _migrate_session_version()
    _migrate_archived()
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
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(tickets_router.router)
app.include_router(comments_router.router)
app.include_router(users_router.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
