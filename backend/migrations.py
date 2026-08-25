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


def _migrate_notification_preferences(engine: Engine) -> None:
    """notification_preferences table — added with the settings panel (#55).

    ``create_all`` already builds brand-new tables on a fresh boot; this makes the
    add explicit and idempotent (``checkfirst`` no-ops when the table exists) so a
    database that predates the model still gains the table on the next startup."""
    from models import NotificationPreference

    NotificationPreference.__table__.create(bind=engine, checkfirst=True)


def _migrate_is_resolver_bot(engine: Engine) -> None:
    """users.is_resolver_bot — added so the resolver bot is recognized as a
    trusted control-tag identity by a DB flag instead of a synced env id."""
    _add_column(engine, "users", "is_resolver_bot", "BOOLEAN NOT NULL DEFAULT 0")


def _migrate_api_key_scopes(engine: Engine) -> None:
    """api_keys.scopes — added with the stingray CLI's `cli` scope.

    Existing keys default to '' (no scopes), so nothing gains authority on upgrade."""
    _add_column(engine, "api_keys", "scopes", "VARCHAR NOT NULL DEFAULT ''")


def _migrate_agent_runs(engine: Engine) -> None:
    """agent_runs table — added with resolver token-usage surfacing (#56).

    A brand-new table, so `create_all` already builds it on a fresh DB; this
    step backfills it on a database that predates the model. `checkfirst=True`
    makes it a no-op when the table already exists (idempotent)."""
    from models import AgentRun
    AgentRun.__table__.create(bind=engine, checkfirst=True)


def _migrate_resolver_settings(engine: Engine) -> None:
    """resolver_settings table — added with UI-managed resolver config.

    A brand-new table, so `create_all` builds it on a fresh DB; this backfills
    it on databases that predate the model. `checkfirst=True` no-ops when the
    table already exists (idempotent)."""
    from models import ResolverSettings
    ResolverSettings.__table__.create(bind=engine, checkfirst=True)


def _migrate_resolver_instances(engine: Engine) -> None:
    """resolver_instances table — added with the resolver-manager registry.

    A brand-new table, so `create_all` builds it on a fresh DB; this backfills
    it on databases that predate the model. `checkfirst=True` no-ops when the
    table already exists (idempotent)."""
    from models import ResolverInstance
    ResolverInstance.__table__.create(bind=engine, checkfirst=True)



def _migrate_saved_views(engine: Engine) -> None:
    """saved_views table — added with the dashboard filter panel.

    A brand-new table, so `create_all` builds it on a fresh DB; this backfills
    it on databases that predate the model. `checkfirst=True` no-ops when the
    table already exists (idempotent)."""
    from models import SavedView
    SavedView.__table__.create(bind=engine, checkfirst=True)


def _migrate_outbox(engine: Engine) -> None:
    """outbox table — added with the transactional event bus.

    A brand-new table, so `create_all` builds it on a fresh DB; this backfills
    it on databases that predate the model. `checkfirst=True` no-ops when the
    table already exists (idempotent)."""
    from models import Outbox
    Outbox.__table__.create(bind=engine, checkfirst=True)


def _migrate_webhooks(engine: Engine) -> None:
    """webhooks + webhook_deliveries tables — added with webhook subscriptions.

    Brand-new tables, so `create_all` builds them on a fresh DB; this backfills
    them on databases that predate the models. `checkfirst=True` no-ops when a
    table already exists (idempotent). Order matters: webhook_deliveries carries
    a foreign key onto webhooks."""
    from models import Webhook, WebhookDelivery
    Webhook.__table__.create(bind=engine, checkfirst=True)
    WebhookDelivery.__table__.create(bind=engine, checkfirst=True)


# Ordered list of migrations applied on startup (after create_all).
MIGRATIONS = [
    _migrate_session_version,
    _migrate_archived,
    _migrate_notification_preferences,
    _migrate_is_resolver_bot,
    _migrate_api_key_scopes,
    _migrate_agent_runs,
    _migrate_resolver_settings,
    _migrate_resolver_instances,
    _migrate_saved_views,
    _migrate_outbox,
    _migrate_webhooks,
]


def run_migrations(engine: Engine) -> None:
    """Run all migrations in order. Safe to call on every startup."""
    for migration in MIGRATIONS:
        migration(engine)
