#!/usr/bin/env python3
"""File a Stingray ticket from a resolver run — a validated alternative to
hand-writing `curl`.

The agent invokes this via Bash from inside its worktree. The URL, API key and
bot identity come from the resolver's `.env` (never the prompt), the enum/required
fields are validated up front, and `--code-block` reads the exact lines off disk
so multi-line code never has to survive JSON/shell escaping.

  ./file_ticket.py --type task --title "Flaky retry test" \
      --description "Failed twice this week" --priority low --tag backend

  ./file_ticket.py --type code_review --title "Review: auth refactor" \
      --priority high --tag backend --code-block backend/auth.py:python:60-66

A `--code-block` is `PATH:LANGUAGE:START-END` (or `PATH:LANGUAGE:LINE`); PATH is
read relative to --root (default: the current directory) and stored as given, so
run it from the repo root and pass repo-relative paths.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

import audit
from config import Config
from stingray import StingrayClient

HERE = Path(__file__).resolve().parent

TYPES = ("code_review", "task")
PRIORITIES = ("low", "medium", "high", "critical")

# Delegation control tags (mirrors resolve_tickets.py / backend control_tags.py).
TAG_DELEGATE = "delegate"
PARENT_PREFIX = "parent:"
REVIEW_BY_PREFIX = "review-by:"
REPO_PREFIX = "repo:"


def inherited_parent_tags(client, parent_id: int) -> list[str]:
    """Tags a delegated sub-task must inherit from its parent so its assignee can act:

    - ``review-by:<parent.created_by>`` — who the finished PR is handed back to (the
      human who asked for the audit).
    - the parent's ``repo:<name>`` — which repo to check out. The assignee can't
      discover this from the parent itself (ticket read access is restricted to a
      ticket's creator/assignee, and the worker is neither), so without this the
      worker fails with "no repo specified" and bounces the child back.

    We stamp these at creation because the lead bot filing the child *can* read the
    parent (it's assigned to it during the run). Best-effort: empty if the parent
    can't be read, leaving the defaults."""
    try:
        parent = client.get_ticket(parent_id)
    except Exception:
        return []
    tags: list[str] = []
    owner = parent.get("created_by")
    if owner:
        tags.append(f"{REVIEW_BY_PREFIX}{owner}")
    tags += [t for t in (parent.get("tags") or []) if t.startswith(REPO_PREFIX)]
    return tags


def user_id(value: str) -> int:
    """argparse type for --assign: a numeric user id. A username like 'admin'
    can't be resolved here (the resolver is a non-admin bot), so fail with an
    actionable message instead of argparse's opaque 'invalid int value'."""
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"needs a numeric user id (got {value!r}); omit --assign to assign to yourself"
        )


def parse_code_block(spec: str, root: Path) -> dict:
    """Turn a `PATH:LANGUAGE:START-END` spec into a ticket code_block, reading the
    exact lines off disk so their content never has to be escaped by hand."""
    head, _, rest = spec.rpartition(":")
    # rpartition splits on the LAST colon; one more split peels the language off,
    # leaving PATH intact even if it contained a colon (it normally won't).
    filename, _, language = head.rpartition(":")
    if not filename or not language or not rest:
        raise ValueError(
            f"--code-block {spec!r} must be PATH:LANGUAGE:START-END "
            "(e.g. backend/auth.py:python:60-66)"
        )

    start_s, _, end_s = rest.partition("-")
    try:
        start = int(start_s)
        end = int(end_s) if end_s else start
    except ValueError:
        raise ValueError(f"--code-block {spec!r}: line range must be numbers, got {rest!r}")
    if start < 1 or end < start:
        raise ValueError(f"--code-block {spec!r}: need 1 <= start <= end, got {start}-{end}")

    file_path = (root / filename)
    if not file_path.is_file():
        raise ValueError(f"--code-block {spec!r}: file not found: {file_path}")
    lines = file_path.read_text(encoding="utf-8").splitlines()
    if end > len(lines):
        raise ValueError(
            f"--code-block {spec!r}: file {filename} has {len(lines)} lines, "
            f"can't reach line {end}"
        )

    return {
        "filename": filename,
        "language": language,
        "line_start": start,
        "line_end": end,
        "content": "\n".join(lines[start - 1:end]),
    }


def build_payload(args: argparse.Namespace) -> dict:
    """Validate args and assemble the POST body. Raises ValueError on bad input."""
    title = (args.title or "").strip()
    if not title:
        raise ValueError("--title must not be empty")

    specs = args.code_block or []
    if specs and args.type != "code_review":
        raise ValueError("--code-block is only valid with --type code_review")

    root = Path(args.root).resolve()
    tags = list(args.tag or [])

    # `parent` may be absent on a Namespace from the /ticket directive parser, which
    # doesn't expose --parent (delegation is filed via the full CLI parser, not /ticket).
    parent = getattr(args, "parent", None)
    if parent is not None:
        # A delegated sub-task. The `parent:<id>` link makes it self-driving: the
        # worker that picks it up plans it and lets its review AI auto-approve the
        # plan (falling back to dangerous, no-plan implement when no review AI is
        # configured) — see resolve_tickets.do_plan/process. We deliberately do NOT
        # force `dangerous` here anymore: the old behavior implemented children with
        # no plan and no review at all. Keep it a LEAF: a child may never carry
        # `delegate`, so it can't fan out further (one level only).
        if TAG_DELEGATE in tags:
            raise ValueError(
                "a delegated sub-task (--parent) may not be tagged 'delegate' — "
                "fan-out is one level only"
            )
        tags.append(f"{PARENT_PREFIX}{parent}")

    payload: dict = {
        "type": args.type,
        "title": title,
        "description": args.description or "",
        "priority": args.priority,
        "tags": tags,
        "code_blocks": [parse_code_block(s, root) for s in specs],
    }
    if args.assign is not None:
        payload["assigned_to"] = args.assign
    return payload


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="File a Stingray ticket from a resolver run (validated, no curl).",
    )
    p.add_argument("--type", required=True, choices=TYPES, help="ticket type")
    p.add_argument("--title", required=True, help="ticket title (required, non-empty)")
    p.add_argument("--description", default="", help="ticket body")
    p.add_argument("--priority", default="medium", choices=PRIORITIES, help="default: medium")
    p.add_argument("--tag", action="append", metavar="TAG", help="repeatable")
    p.add_argument("--assign", type=user_id, metavar="USER_ID", help="assign to this user id")
    p.add_argument(
        "--parent", type=int, metavar="TICKET_ID",
        help="file as a delegated sub-task of this ticket: links it (parent:<id>) so "
             "the assignee plans it and its review AI auto-approves the plan (with a "
             "dangerous no-plan fallback when no review AI is configured), and forbids "
             "re-delegation (one level only)",
    )
    p.add_argument(
        "--code-block", action="append", dest="code_block", metavar="PATH:LANG:START-END",
        help="repeatable; reads the lines from disk (code_review only)",
    )
    p.add_argument("--root", default=".", help="base dir for --code-block paths (default: cwd)")
    p.add_argument("--dry-run", action="store_true", help="print the payload, don't POST")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        payload = build_payload(args)
    except ValueError as exc:
        parser.error(str(exc))

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    cfg = Config.load()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    logger = audit.setup_logging(cfg, sweep_id=f"file-ticket-{ts}")
    client = StingrayClient(cfg.stingray_url, cfg.api_key,
                            max_retries=cfg.stingray_max_retries, logger=logger)

    if args.parent is not None:
        # Fan-out cap: bound how many sub-tasks one delegation run may file, so a single
        # orchestration can't spawn unbounded tickets / agent cost. Count this parent's
        # existing children (created_by this bot, so visible to its non-admin key).
        if cfg.max_delegations > 0:
            existing = sum(1 for _ in client.iter_tickets(tag=f"{PARENT_PREFIX}{args.parent}"))
            if existing >= cfg.max_delegations:
                print(
                    f"error: delegation cap reached — ticket #{args.parent} already has "
                    f"{existing} sub-task(s) (RESOLVER_MAX_DELEGATIONS={cfg.max_delegations})",
                    file=sys.stderr,
                )
                return 1
        # Inherit review owner + the target repo from the parent while we can still
        # read it — the worker that finishes the child cannot.
        for t in inherited_parent_tags(client, args.parent):
            if t not in payload["tags"]:
                payload["tags"].append(t)

    try:
        ticket = client.create_ticket(**payload)
    except requests.HTTPError as exc:
        resp = exc.response
        detail = resp.text if resp is not None else str(exc)
        status = resp.status_code if resp is not None else "?"
        print(f"error: filing ticket failed ({status})\n{detail}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"error: could not reach Stingray: {exc}", file=sys.stderr)
        return 1

    print(f"created ticket #{ticket['id']}: {ticket['title']}")
    print(f"{cfg.stingray_url}/tickets/{ticket['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
