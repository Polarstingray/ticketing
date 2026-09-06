"""One record per resolver, joined from every place the truth actually lives.

Answering "is this resolver healthy?" currently means reading four things at
once: systemd for the units, the log files for what the last sweep did, git for
which revision the units are executing, and the server for whether an admin has
overridden the config. This module does that join so a command can print it.

Nothing here is cached. Every field is re-derived on each call, because a stale
answer to "is it running" is worse than a slow one.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from stingray_cli.station import units
from stingray_cli.station.identity import read_env
from stingray_cli.station.inventory import Resolver

_GIT_TIMEOUT = 15
# How much of a log to read when looking for the last interesting line. These
# files are appended to forever (rotation is logrotate's job), so reading the
# whole thing would be unbounded.
_TAIL_BYTES = 64 * 1024


@dataclass
class GitInfo:
    branch: str = ""
    sha: str = ""
    dirty: bool = False
    error: str = ""

    def __str__(self) -> str:
        if self.error:
            return f"({self.error})"
        if not self.sha:
            return "(unknown)"
        return f"{self.branch or 'detached'} @{self.sha}" + (" ⚠ dirty" if self.dirty else "")


@dataclass
class ResolverStatus:
    resolver: Resolver
    timer: units.UnitState
    listener: units.UnitState
    sweep: units.UnitState
    next_sweep: str = ""
    last_sweep_line: str = ""
    last_sweep_at: datetime | None = None
    listener_line: str = ""
    listener_at: datetime | None = None
    connected: bool | None = None
    git: GitInfo = field(default_factory=GitInfo)
    env_present: bool = True
    bot_user_id: int | None = None
    stingray_url: str = ""

    @property
    def managed(self) -> bool:
        """True when both units are enabled or running.

        Deliberately not `LoadState` — see `UnitState.installed`. A half-enabled
        identity is a real state and shows as "partial" rather than being
        rounded to either end.
        """
        return self.timer.installed and self.listener.installed

    @property
    def running(self) -> bool:
        return self.timer.ok and self.listener.ok

    @property
    def summary(self) -> str:
        if self.running:
            return "running" if self.connected is not False else "running (stream down)"
        if self.managed:
            return "stopped"
        if self.timer.installed or self.listener.installed:
            return "partial"
        return "not installed"


def _tail(path: Path) -> list[str]:
    """The last lines of a file, without reading all of it."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(-_TAIL_BYTES, 2)
                fh.readline()  # drop the partial line the seek landed inside
            return fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _last_matching(lines: list[str], *needles: str) -> str:
    for line in reversed(lines):
        if any(n in line for n in needles):
            return line.strip()
    return ""


def git_info(checkout: Path) -> GitInfo:
    """Which revision the units are actually executing.

    Worth surfacing because the units run out of a live working tree: checking
    out a branch changes what every resolver on the host runs, and a dirty tree
    means what is running is not any committed revision at all.
    """
    def _git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(checkout), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    if not checkout.is_dir():
        return GitInfo(error="checkout missing")
    try:
        sha = _git("rev-parse", "--short", "HEAD")
        if not sha:
            return GitInfo(error="not a git checkout")
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
        return GitInfo(branch="" if branch == "HEAD" else branch, sha=sha, dirty=dirty)
    except (OSError, subprocess.SubprocessError) as exc:
        return GitInfo(error=f"git failed: {exc}")


def collect(resolver: Resolver) -> ResolverStatus:
    """Everything known about one identity, right now."""
    env = read_env(resolver.env_path)
    bot = env.get("RESOLVER_BOT_USER_ID", "")

    sweep_lines = _tail(resolver.sweep_log)
    listen_lines = _tail(resolver.listener_log)
    listener_line = _last_matching(
        listen_lines, "connected to", "disconnected", "poked", "shutting down")
    connected: bool | None = None
    if listener_line:
        # "connected" and "poked" both mean the stream was up as of that line;
        # "disconnected" means it was not. Anything else leaves it unknown
        # rather than guessing, since a wrong green light is worse than none.
        if "disconnected" in listener_line:
            connected = False
        elif "connected to" in listener_line or "poked" in listener_line:
            connected = True

    return ResolverStatus(
        resolver=resolver,
        timer=units.unit_state(resolver.timer_unit),
        listener=units.unit_state(resolver.listener_unit),
        sweep=units.unit_state(resolver.sweep_unit),
        next_sweep=units.timer_next(resolver),
        last_sweep_line=_last_matching(sweep_lines, "sweep done", "sweep start", "FAILED"),
        last_sweep_at=_mtime(resolver.sweep_log),
        listener_line=listener_line,
        listener_at=_mtime(resolver.listener_log),
        connected=connected,
        git=git_info(resolver.checkout),
        env_present=resolver.env_path.exists(),
        bot_user_id=int(bot) if bot.isdigit() else None,
        stingray_url=env.get("STINGRAY_URL", ""),
    )
