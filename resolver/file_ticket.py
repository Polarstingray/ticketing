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
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

import audit
from config import Config
from stingray import StingrayClient
from stingray_client.tickets import (
    PARENT_PREFIX,
    PRIORITIES,
    REPO_PREFIX,
    REVIEW_BY_PREFIX,
    TAG_DELEGATE,
    TYPES,
    derive_repo_tag,
    has_repo_tag,
    inherited_parent_tags,
    parse_code_block,
)
from stingray_client.tickets import build_payload as _build_payload

HERE = Path(__file__).resolve().parent

__all__ = [
    "TYPES", "PRIORITIES", "TAG_DELEGATE", "PARENT_PREFIX", "REVIEW_BY_PREFIX",
    "REPO_PREFIX", "inherited_parent_tags", "derive_repo_tag", "has_repo_tag",
    "parse_code_block", "build_payload", "user_id", "main",
]


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


def _repo_for(args: argparse.Namespace) -> "str | None":
    """Which repo a ticket filed from this run should name.

    Inside a sweep the run is authoritative: resolve_tickets.process exports the repo
    it is working ON as ``STINGRAY_TICKET_REPO``, and that wins over anything the
    agent passes. Outside a sweep (a human at a shell) the variable is unset and
    explicit ``--repo`` wins as always.

    Deferring to ``--repo`` inside a sweep is what mis-tagged #46: the agent was told
    to `cd` into the resolver's own checkout to run this script and duly passed
    ``--repo resolver-ticketing``, so a $16 implement run happened in the resolver's
    clone — whose origin has no push credentials — and the work was stranded on a
    local branch. An agent's cwd is never authoritative about which project a ticket
    is about; the run already knows, and it knew here. (#42/#43 were the same bug via
    the fall-through to cwd-based derivation.)

    The inherited value is still suppressible: ``--no-repo`` opts out inside a sweep
    exactly as it opts out of derivation."""
    inherited = os.environ.get("STINGRAY_TICKET_REPO", "").strip()
    explicit = getattr(args, "repo", None)
    if getattr(args, "no_repo", False):
        return None
    if inherited:
        if explicit and explicit != inherited:
            print(f"note: ignoring --repo {explicit!r}; this run is working on "
                  f"{inherited!r} (pass --no-repo to file an untagged ticket)",
                  file=sys.stderr)
        return inherited
    return explicit or None


def build_payload(args: argparse.Namespace) -> dict:
    """Validate args and assemble the POST body. Raises ValueError on bad input.

    A thin Namespace adapter over ``stingray_client.tickets.build_payload``. It
    stays here because ``resolve_tickets``' ``/ticket`` directive parser builds a
    Namespace and calls this — keeping the adapter means that path is untouched.
    That parser also doesn't expose every flag, hence the ``getattr`` defaults.
    """
    return _build_payload(
        type=args.type,
        title=args.title,
        description=args.description,
        priority=args.priority,
        tags=list(args.tag or []),
        code_block_specs=args.code_block or [],
        root=args.root,
        repo=_repo_for(args),
        no_repo=getattr(args, "no_repo", False),
        rev=getattr(args, "rev", None),
        branch=getattr(args, "branch", None),
        parent=getattr(args, "parent", None),
        assign=args.assign,
        warn=lambda msg: print(msg, file=sys.stderr),
    )


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
        "--repo", metavar="NAME",
        help="target repo (stored as repo:<NAME>); the resolver needs it to check out "
             "code. Defaults to the git checkout at --root. Ignored inside a resolver "
             "sweep, which already knows the repo it is working on",
    )
    p.add_argument(
        "--no-repo", action="store_true",
        help="don't tag a repo (a review of pasted code with no checkout)",
    )
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
    p.add_argument("--rev", metavar="SHA",
                   help="pin the ticket to this commit (rev:<SHA>) so a review reads "
                        "the code as of that commit, not whatever is checked out")
    p.add_argument("--branch", metavar="NAME",
                   help="branch the pinned commit is on (branch:<NAME>); a fix stacks "
                        "on it and its PR targets it")
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
            if t.startswith(REPO_PREFIX) and has_repo_tag(payload["tags"]):
                continue  # an explicit --repo on the child wins over the parent's
            if t not in payload["tags"]:
                payload["tags"].append(t)
        # Parent unreadable or itself untagged: fall back to the checkout we're in,
        # so a sub-task never lands without a repo to check out.
        if not args.no_repo and not has_repo_tag(payload["tags"]):
            derived = derive_repo_tag(Path(args.root).resolve())
            if derived:
                payload["tags"].append(derived)
                print(f"auto-tagged {derived} (parent #{args.parent} named no repo)",
                      file=sys.stderr)

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
