"""The demo seed must produce rows the API can actually serialize.

`seed_demo` writes rows directly through SQLAlchemy, bypassing the Pydantic
schemas that requests go through. The columns are plain strings, so SQLite
happily stores a `type` of "bug" — and then GET /tickets 500s, because the
response model only admits the TicketType vocabulary. (That bug shipped once;
this test is why it can't again.)

The seed runs in a subprocess against its own DATABASE_PATH: the `database`
module binds DATABASE_PATH at import time, and the rest of the suite shares one
database that `--force` would wipe.
"""
import os
import sqlite3
import subprocess
import sys
from typing import get_args

from models import TicketPriority, TicketStatus, TicketType
from schemas import AgentName, AgentPhaseName, AgentRunStatusName

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))


def _seed_into(db_path):
    env = {**os.environ, "DATABASE_PATH": str(db_path)}
    proc = subprocess.run(
        [sys.executable, "-m", "seed_demo"],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"seed_demo failed:\n{proc.stdout}\n{proc.stderr}"
    return sqlite3.connect(db_path)


def _column(conn, table, column):
    return [r[0] for r in conn.execute(f"SELECT {column} FROM {table}")]


def test_seeded_tickets_use_valid_vocabularies(tmp_path):
    conn = _seed_into(tmp_path / "demo.db")
    try:
        types = _column(conn, "tickets", "type")
        statuses = _column(conn, "tickets", "status")
        priorities = _column(conn, "tickets", "priority")
    finally:
        conn.close()

    assert types, "seed produced no tickets"
    assert set(types) <= {t.value for t in TicketType}
    assert set(statuses) <= {s.value for s in TicketStatus}
    assert set(priorities) <= {p.value for p in TicketPriority}


def test_seeded_agent_runs_use_valid_vocabularies(tmp_path):
    conn = _seed_into(tmp_path / "demo.db")
    try:
        agents = _column(conn, "agent_runs", "agent")
        phases = _column(conn, "agent_runs", "phase")
        statuses = _column(conn, "agent_runs", "status")
    finally:
        conn.close()

    assert agents, "seed produced no agent runs"
    assert set(agents) <= set(get_args(AgentName))
    assert set(phases) <= set(get_args(AgentPhaseName))
    assert set(statuses) <= set(get_args(AgentRunStatusName))


def test_seed_refuses_to_clobber_an_existing_database(tmp_path):
    """Guard against pointing the demo seed at a real DB by accident."""
    db_path = tmp_path / "demo.db"
    _seed_into(db_path).close()

    env = {**os.environ, "DATABASE_PATH": str(db_path)}
    proc = subprocess.run(
        [sys.executable, "-m", "seed_demo"],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "refusing to seed" in proc.stderr.lower()
