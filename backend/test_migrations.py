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


def test_adds_missing_columns():
    engine, path = _legacy_engine()
    try:
        assert "session_version" not in _columns(engine, "users")
        assert "archived" not in _columns(engine, "tickets")

        run_migrations(engine)

        assert "session_version" in _columns(engine, "users")
        assert "archived" in _columns(engine, "tickets")
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
