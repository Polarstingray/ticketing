"""Drive one eval case through the resolver's plan→approve→implement lifecycle.

The resolver is the real, unmodified `resolve_tickets.py`, invoked per-ticket as a
subprocess (matching cron). Between sweeps this module plays the *human*: when the
resolver hands a ticket back awaiting plan approval, it reassigns the ticket to the bot
and posts `/approve`, exactly as a person would, then runs the next sweep. It stops when
the ticket is implemented, failed, or a step cap is hit.
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

RESOLVER_DIR = Path(__file__).resolve().parents[2]

# Markers / state tags mirrored from resolve_tickets.py (kept as literals so the driver
# doesn't import the resolver's module-level state).
IMPL_MARKER = "✅ **Implemented**"
FAIL_MARKER = "⚠️ Resolver could not complete"
TAG_AWAIT_PLAN = "resolver:awaiting-plan-approval"
TAG_AWAIT_PR = "resolver:awaiting-pr-review"


@dataclass
class DriveResult:
    outcome: str                 # "produced" | "failed" | "timed_out" | "error"
    steps: int
    branch: str                  # claude/ticket-<id>
    wall_s: float
    sweep_rc: list[int] = field(default_factory=list)


def _run_resolver(env_file: Path, ticket_id: int, python: str, timeout: int) -> int:
    """One resolver sweep over a single ticket. Returns the process exit code."""
    proc = subprocess.run(
        [python, "resolve_tickets.py", "--ticket", str(ticket_id)],
        cwd=str(RESOLVER_DIR),
        env={**_base_env(env_file)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    return proc.returncode


def _base_env(env_file: Path) -> dict:
    import os
    env = dict(os.environ)
    env["RESOLVER_ENV_FILE"] = str(env_file)
    return env


def _comment_bodies(client, ticket_id: int) -> list[str]:
    return [(c.get("body") or "") for c in client.list_comments(ticket_id)]


def drive_case(client, bot_user_id: int, ticket_id: int, env_file: Path, *,
               python: str | None = None, max_steps: int = 4,
               sweep_timeout: int = 3600) -> DriveResult:
    """Run the lifecycle for one ticket. `client` is a StingrayClient using the ADMIN
    key (it acts as the human)."""
    python = python or sys.executable
    branch = f"claude/ticket-{ticket_id}"
    start = time.time()
    rcs: list[int] = []

    for step in range(1, max_steps + 1):
        try:
            rcs.append(_run_resolver(env_file, ticket_id, python, sweep_timeout))
        except subprocess.TimeoutExpired:
            return DriveResult("error", step, branch, time.time() - start, rcs)

        ticket = client.get_ticket(ticket_id)
        bodies = _comment_bodies(client, ticket_id)
        tags = ticket.get("tags", [])

        if any(IMPL_MARKER in b for b in bodies) or TAG_AWAIT_PR in tags:
            return DriveResult("produced", step, branch, time.time() - start, rcs)
        if any(FAIL_MARKER in b for b in bodies):
            return DriveResult("failed", step, branch, time.time() - start, rcs)

        # Plan posted and handed back to the human → approve and reassign to the bot so
        # the next sweep implements it.
        if TAG_AWAIT_PLAN in tags and ticket.get("assigned_to") != bot_user_id:
            client.add_comment(ticket_id, "/approve")
            client.update_ticket(ticket_id, assigned_to=bot_user_id)
            continue

        # Otherwise the resolver left it mid-flight (e.g. still planning, or assigned to
        # itself); just sweep again.

    return DriveResult("timed_out", max_steps, branch, time.time() - start, rcs)
