"""Migrations are idempotent and add missing columns to legacy databases."""
import os
import tempfile

from sqlalchemy import create_engine, inspect, text

from migrations import run_migrations


def _legacy_engine():
    """A throwaway SQLite db shaped like a pre-migration schema: a `users` table
    without `session_version` and a `tickets` table without `archived`."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)"))
        conn.execute(text("CREATE TABLE tickets (id INTEGER PRIMARY KEY, title TEXT)"))
    return engine, path


def _columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _tables(engine):
    return set(inspect(engine).get_table_names())


def test_adds_missing_columns():
    engine, path = _legacy_engine()
    try:
        assert "session_version" not in _columns(engine, "users")
        assert "archived" not in _columns(engine, "tickets")

        run_migrations(engine)

        assert "session_version" in _columns(engine, "users")
        assert "archived" in _columns(engine, "tickets")
        # The resolver-bot trust flag is added to legacy users tables.
        assert "is_resolver_bot" in _columns(engine, "users")
        # The settings panel's table is created (idempotently) by a migration too.
        assert "notification_preferences" in inspect(engine).get_table_names()
    finally:
        os.unlink(path)


def test_idempotent():
    engine, path = _legacy_engine()
    try:
        run_migrations(engine)
        # Running again must not raise (columns already present).
        run_migrations(engine)
        assert "session_version" in _columns(engine, "users")
    finally:
        os.unlink(path)


def test_creates_agent_runs_table():
    """On a DB predating the AgentRun model, run_migrations creates the
    agent_runs table and is idempotent on a second call (#56)."""
    engine, path = _legacy_engine()
    try:
        assert "agent_runs" not in _tables(engine)
        run_migrations(engine)
        assert "agent_runs" in _tables(engine)
        cols = _columns(engine, "agent_runs")
        assert {"ticket_id", "phase", "agent", "cost_usd", "input_tokens"} <= cols
        # Second run must not raise (table already present).
        run_migrations(engine)
        assert "agent_runs" in _tables(engine)
    finally:
        os.unlink(path)


def test_skips_absent_tables():
    """With no tables at all, migrations are a no-op (create_all makes new
    tables; there's nothing to ALTER)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    try:
        run_migrations(engine)  # should not raise
    finally:
        os.unlink(path)


def test_adds_api_key_scopes():
    """api_keys.scopes is backfilled on a legacy DB, defaulting to no scopes so
    existing keys gain no authority on upgrade."""
    engine, path = _legacy_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE api_keys (id INTEGER PRIMARY KEY, name TEXT)"
            ))
            conn.execute(text("INSERT INTO api_keys (id, name) VALUES (1, 'legacy')"))
        assert "scopes" not in _columns(engine, "api_keys")

        run_migrations(engine)
        assert "scopes" in _columns(engine, "api_keys")

        with engine.begin() as conn:
            existing = conn.execute(text("SELECT scopes FROM api_keys WHERE id = 1")).scalar()
        assert existing == ""

        run_migrations(engine)  # idempotent
        assert "scopes" in _columns(engine, "api_keys")
    finally:
        os.unlink(path)
