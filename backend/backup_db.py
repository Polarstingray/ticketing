"""Online SQLite backup with rotation.

Uses SQLite's backup API (``Connection.backup``), which takes a consistent
snapshot while the app is still serving requests — no need to stop the
container. Intended to be run on a schedule (cron, a systemd timer, or a tiny
compose sidecar); see the README "Backups" section.

Usage:
    python backup_db.py [--db PATH] [--out DIR] [--keep N]

Defaults mirror the app: ``--db`` falls back to ``DATABASE_PATH`` (or the
in-tree dev path), ``--out`` to ``<db_dir>/backups``, keeping the 14 newest.
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = os.environ.get(
    "DATABASE_PATH", os.path.join(os.path.dirname(__file__), "data", "stingray.db")
)


def make_backup(db_path: Path, out_dir: Path) -> Path:
    """Write a timestamped consistent snapshot of ``db_path`` into ``out_dir``."""
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = out_dir / f"stingray-{stamp}.db"

    # Read-only source; the backup API copies pages under a shared lock so it is
    # consistent even with concurrent writers.
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def prune(out_dir: Path, keep: int) -> list[Path]:
    """Delete all but the ``keep`` newest backups; return what was removed."""
    backups = sorted(out_dir.glob("stingray-*.db"), key=lambda p: p.name)
    removed = backups[:-keep] if keep > 0 and len(backups) > keep else []
    for old in removed:
        old.unlink()
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the Stingray SQLite database.")
    parser.add_argument("--db", default=DEFAULT_DB, help="path to stingray.db")
    parser.add_argument("--out", default=None, help="backup directory (default: <db_dir>/backups)")
    parser.add_argument("--keep", type=int, default=14, help="number of backups to retain")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    out_dir = Path(args.out) if args.out else db_path.parent / "backups"

    dest = make_backup(db_path, out_dir)
    removed = prune(out_dir, args.keep)
    print(f"backup written: {dest}")
    if removed:
        print(f"pruned {len(removed)} old backup(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
