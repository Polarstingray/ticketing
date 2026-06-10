"""Lightweight, dependency-free schema migrations.

This project intentionally avoids Alembic at its current scale. ``create_all``
creates *missing tables* but never alters an existing one, so when a column is
added to a model we need a small idempotent step that adds it to databases that
predate the change.

Each migration is a function taking the live ``Engine`` that:
  * inspects the current schema and does nothing if already applied (idempotent),
  * otherwise performs the change (typically an ``ALTER TABLE ... ADD COLUMN``).

To add one: write ``def _migrate_<thing>(engine): ...`` following the pattern
below, then append it to ``MIGRATIONS``. They run in order on every startup, so
they must stay cheap and idempotent. Adopt Alembic only if this list ever grows
unwieldy or needs data backfills / column drops (which SQLite can't ALTER away).
"""
import logging

from sqlalchemy import Engine, inspect, text

log = logging.getLogger("stingray.migrations")


def _table_columns(engine: Engine, table: str) -> set[str]:
    """Column names for ``table``; empty set if the table doesn't exist yet
    (``create_all`` will create brand-new tables, so there's nothing to migrate)."""
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def _add_column(engine: Engine, table: str, column: str, ddl: str) -> None:
    """Add ``column`` to ``table`` if absent. ``ddl`` is the column definition,
    e.g. ``"INTEGER NOT NULL DEFAULT 0"``."""
    cols = _table_columns(engine, table)
    # Empty set => the table itself doesn't exist yet; create_all will build it
    # with the column already present, so there's nothing to ALTER.
    if not cols or column in cols:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    log.info("migration: added %s.%s", table, column)


def _migrate_session_version(engine: Engine) -> None:
    """users.session_version — added with revocable sessions (#14)."""
    _add_column(engine, "users", "session_version", "INTEGER NOT NULL DEFAULT 0")


def _migrate_archived(engine: Engine) -> None:
    """tickets.archived — added with the ticket archive (#20)."""
    _add_column(engine, "tickets", "archived", "BOOLEAN NOT NULL DEFAULT 0")


def _migrate_agent_runs(engine: Engine) -> None:
    """agent_runs table — added with resolver token-usage surfacing (#56).

    A brand-new table, so `create_all` already builds it on a fresh DB; this
    step backfills it on a database that predates the model. `checkfirst=True`
    makes it a no-op when the table already exists (idempotent)."""
    from models import AgentRun
    AgentRun.__table__.create(bind=engine, checkfirst=True)


# Ordered list of migrations applied on startup (after create_all).
MIGRATIONS = [
    _migrate_session_version,
    _migrate_archived,
    _migrate_agent_runs,
]


def run_migrations(engine: Engine) -> None:
    """Run all migrations in order. Safe to call on every startup."""
    for migration in MIGRATIONS:
        migration(engine)
