#!/usr/bin/env python3
"""View the resolver's per-ticket plan/implement logs — the raw agent output.

Both resolver identities (claude-bot, gemini-bot) write to the same logs/ dir,
so this shows every bot's runs in one place. It reads loose `ticket-*-*.log`
files and, once a day has been rolled into `logs/archive/<date>.tar.gz`, reads
the log back out of the tarball transparently.

  ./logs.py              list recent runs, newest first
  ./logs.py 42           print ticket 42's latest *implement* log
  ./logs.py 42 --plan    print ticket 42's latest *plan* log
  ./logs.py 42 --review  print ticket 42's latest *review* log
  ./logs.py 42 --all     list every run for ticket 42
  ./logs.py 42 -f        follow the newest matching log (a run in progress)
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_LOGS_DIR = HERE / "logs"

# ticket-<id>-<phase>-<YYYYMMDD-HHMMSS>.log
_LOG_RE = re.compile(r"^ticket-(\d+)-(plan|implement|review)-(\d{8}-\d{6})\.log$")


@dataclass
class LogRef:
    ticket: int
    phase: str            # "plan" | "implement" | "review"
    ts: str               # raw YYYYMMDD-HHMMSS (sorts chronologically)
    name: str             # the file's basename
    path: Path | None = None        # set for a loose file
    archive: Path | None = None     # set when the log lives inside a tarball
    size: int = 0

    @property
    def when(self) -> str:
        try:
            return datetime.strptime(self.ts, "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return self.ts


def _ref_from_name(name: str) -> tuple[int, str, str] | None:
    m = _LOG_RE.match(name)
    if not m:
        return None
    return int(m.group(1)), m.group(2), m.group(3)


def collect_logs(logs_dir: Path, ticket: int | None = None,
                 phase: str | None = None) -> list[LogRef]:
    """All matching per-ticket logs (loose + archived), newest first."""
    refs: list[LogRef] = []
    for path in logs_dir.glob("ticket-*-*.log"):
        parsed = _ref_from_name(path.name)
        if not parsed:
            continue
        tid, ph, ts = parsed
        if (ticket is None or tid == ticket) and (phase is None or ph == phase):
            refs.append(LogRef(tid, ph, ts, path.name, path=path,
                               size=path.stat().st_size if path.exists() else 0))
    for tarball in sorted(logs_dir.glob("archive/*.tar.gz")):
        try:
            with tarfile.open(tarball, "r:gz") as tar:
                for member in tar.getmembers():
                    parsed = _ref_from_name(os.path.basename(member.name))
                    if not parsed:
                        continue
                    tid, ph, ts = parsed
                    if (ticket is None or tid == ticket) and (phase is None or ph == phase):
                        refs.append(LogRef(tid, ph, ts, os.path.basename(member.name),
                                           archive=tarball, size=member.size))
        except (OSError, tarfile.TarError):
            continue
    refs.sort(key=lambda r: r.ts, reverse=True)
    return refs


def read_log(ref: LogRef) -> str:
    """The raw text of a log, whether loose or inside an archive tarball."""
    if ref.path is not None:
        return ref.path.read_text(encoding="utf-8", errors="replace")
    if ref.archive is not None:
        with tarfile.open(ref.archive, "r:gz") as tar:
            fh = tar.extractfile(ref.name)
            if fh is not None:
                return fh.read().decode("utf-8", errors="replace")
    return ""


def _status_hint(ref: LogRef) -> str:
    """Best-effort one-word status from the log tail; blank if unclear."""
    try:
        text = read_log(ref)
    except (OSError, tarfile.TarError):
        return ""
    if "LAUNCH FAILED" in text:
        return "launch-fail"
    tail = text[-2000:].lower()
    if '"error"' in tail or "traceback (most recent call last)" in tail:
        return "error"
    return ""


def _list(logs_dir: Path, ticket: int | None) -> int:
    refs = collect_logs(logs_dir, ticket=ticket)
    if not refs:
        where = f" for ticket {ticket}" if ticket is not None else ""
        print(f"no resolver logs found{where} in {logs_dir}")
        return 0
    for r in refs:
        loc = "" if r.path is not None else f"  ({r.archive.name})"
        status = _status_hint(r)
        status = f"  {status}" if status else ""
        print(f"#{r.ticket:<5} {r.phase:<9} {r.when}  {r.size:>8}B{status}{loc}")
    return 0


def _follow(path: Path) -> int:
    try:
        return subprocess.call(["tail", "-f", str(path)])
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="View resolver per-ticket logs")
    ap.add_argument("ticket", nargs="?", type=int, help="ticket id to show")
    ap.add_argument("--plan", action="store_true", help="show the plan log (default: implement)")
    ap.add_argument("--review", action="store_true", help="show the code-review log")
    ap.add_argument("--all", action="store_true", help="list every run for the ticket")
    ap.add_argument("-f", "--follow", action="store_true", help="follow the newest matching log")
    ap.add_argument("--logs-dir", type=Path, default=DEFAULT_LOGS_DIR,
                    help=f"logs directory (default: {DEFAULT_LOGS_DIR})")
    args = ap.parse_args(argv)

    logs_dir: Path = args.logs_dir
    if not logs_dir.is_dir():
        print(f"logs dir not found: {logs_dir}", file=sys.stderr)
        return 1

    # No ticket, or --all: list mode.
    if args.ticket is None or args.all:
        return _list(logs_dir, args.ticket)

    phase = "review" if args.review else "plan" if args.plan else "implement"
    refs = collect_logs(logs_dir, ticket=args.ticket, phase=phase)
    if not refs:
        print(f"no {phase} log for ticket {args.ticket} in {logs_dir}", file=sys.stderr)
        return 1
    newest = refs[0]
    if args.follow:
        if newest.path is None:
            print(f"(newest {phase} log is archived in {newest.archive.name}; can't follow)",
                  file=sys.stderr)
        else:
            return _follow(newest.path)
    sys.stdout.write(read_log(newest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
