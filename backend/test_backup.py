"""The online backup helper produces a faithful copy and rotates old backups."""
import sqlite3
from pathlib import Path

from backup_db import make_backup, prune


def _make_db(path: Path, rows: int) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


def test_backup_is_a_faithful_copy(tmp_path):
    db = tmp_path / "stingray.db"
    _make_db(db, rows=5)

    dest = make_backup(db, tmp_path / "backups")
    assert dest.exists()

    conn = sqlite3.connect(dest)
    try:
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()
    assert count == 5


def test_prune_keeps_newest(tmp_path):
    out = tmp_path / "backups"
    out.mkdir()
    # Names sort chronologically, so create a known ordering.
    names = [f"stingray-2026010{n}T000000Z.db" for n in range(1, 6)]
    for name in names:
        (out / name).write_bytes(b"")

    removed = prune(out, keep=2)
    assert len(removed) == 3
    remaining = sorted(p.name for p in out.glob("stingray-*.db"))
    assert remaining == names[-2:]


def test_missing_db_raises(tmp_path):
    try:
        make_backup(tmp_path / "nope.db", tmp_path / "backups")
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError for a missing database")
