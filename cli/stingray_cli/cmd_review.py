"""``stingray review`` — file a code-review ticket from a git range."""
from __future__ import annotations

import json
import sys

from stingray_cli import gitctx
from stingray_cli.common import check_tags, client_from, confirm, post_ticket, profile_from
from stingray_cli.config import ConfigError
from stingray_client.tickets import PRIORITIES, build_payload


def add_parser(sub, add_connection_flags) -> None:
    parser = sub.add_parser(
        "review",
        help="file a code-review ticket from a git range",
        description=(
            "Files a code_review ticket whose code blocks are the hunks the range "
            "touched. Defaults to the last commit plus any uncommitted changes."
        ),
    )
    parser.add_argument("range", nargs="?", metavar="RANGE",
                        help="git range (HEAD~3..HEAD), branch, or commit. "
                             "Default: HEAD~1..HEAD plus working-tree changes")
    parser.add_argument("-m", "--title", help="ticket title")
    parser.add_argument("-d", "--description", help="ticket body")
    parser.add_argument("--priority", default="medium", choices=PRIORITIES)
    parser.add_argument("--tag", action="append", metavar="TAG", help="repeatable")
    parser.add_argument("--repo", metavar="NAME",
                        help="target repo (stored as repo:<NAME>); defaults to this checkout")
    parser.add_argument("--no-repo", action="store_true",
                        help="don't tag a repo (the resolver then can't check code out)")
    parser.add_argument("--assign", type=int, metavar="USER_ID", help="assign to this user id")
    parser.add_argument("--assign-bot", action="store_true",
                        help="assign to the profile's bot_user_id")
    parser.add_argument("--staged", action="store_true",
                        help="review staged changes only (pre-commit style)")
    parser.add_argument("--worktree", dest="worktree", action="store_true", default=None,
                        help="fold uncommitted changes into an explicit RANGE")
    parser.add_argument("--no-worktree", dest="worktree", action="store_false",
                        help="ignore uncommitted changes")
    parser.add_argument("--context", type=int, default=3, metavar="N",
                        help="diff context lines per hunk (default: 3)")
    parser.add_argument("--max-blocks", type=int, default=40, metavar="N")
    parser.add_argument("--max-block-lines", type=int, default=400, metavar="N")
    parser.add_argument("--max-total-lines", type=int, default=4000, metavar="N")
    parser.add_argument("--include", action="append", metavar="GLOB", default=[],
                        help="only these paths (repeatable)")
    parser.add_argument("--exclude", action="append", metavar="GLOB", default=[],
                        help="additionally skip these paths (repeatable)")
    parser.add_argument("--describe", action="store_true",
                        help="use a local agent to write the title/description")
    parser.add_argument("--require-describe", action="store_true",
                        help="fail instead of falling back if --describe doesn't work")
    parser.add_argument("--agent", metavar="NAME", help="agent for --describe (claude|opencode)")
    parser.add_argument("-C", "--root", default=".", help="run as if from this directory")
    parser.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    parser.add_argument("--dry-run", action="store_true", help="print the payload, don't POST")
    add_connection_flags(parser)
    parser.set_defaults(func=cmd_review)


def cmd_review(args) -> int:
    tags = list(args.tag or [])
    # Fail on an unsettable tag before diffing or calling an agent.
    check_tags(tags)

    root = gitctx.repo_root(args.root)
    change = gitctx.resolve_range(root, args.range, staged=args.staged,
                                  include_worktree=args.worktree)

    result = gitctx.collect_blocks(
        change,
        context=args.context,
        max_blocks=args.max_blocks,
        max_block_lines=args.max_block_lines,
        max_total_lines=args.max_total_lines,
        excludes=gitctx.DEFAULT_EXCLUDES + tuple(args.exclude),
        includes=tuple(args.include),
    )

    if not result.blocks:
        print(f"error: no reviewable changes in {change.description}", file=sys.stderr)
        return 1

    title = args.title
    description = args.description
    priority = args.priority

    if args.describe:
        from stingray_cli.describe import describe_change
        suggestion = describe_change(
            change, agent=args.agent, required=args.require_describe,
            profile=_profile_or_none(args),
        )
        if suggestion:
            title = title or suggestion.title
            description = description or suggestion.description
            if args.priority == "medium" and suggestion.priority:
                priority = suggestion.priority
            tags += [t for t in suggestion.tags if t not in tags]

    title = title or gitctx.auto_title(change)
    description = description or gitctx.auto_description(change, result)

    payload = build_payload(
        type="code_review",
        title=title,
        description=description,
        priority=priority,
        tags=tags,
        code_blocks=result.blocks,
        root=root,
        repo=args.repo,
        no_repo=args.no_repo,
        assign=_assignee(args),
    )

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    _summarize(change, payload, result)
    if not confirm("File this ticket?", assume_yes=args.yes):
        print("aborted", file=sys.stderr)
        return 1

    client, profile = client_from(args)
    return post_ticket(client, profile, payload)


def _profile_or_none(args):
    """The profile, if one resolves. --describe is local, so it must still work
    when there are no credentials yet (only filing needs them)."""
    try:
        return profile_from(args)
    except ConfigError:
        return None


def _assignee(args) -> int | None:
    if args.assign is not None:
        return args.assign
    if not args.assign_bot:
        return None
    profile = profile_from(args)
    if profile.bot_user_id is None:
        raise ConfigError(
            "--assign-bot needs a bot user id. Store one with: "
            f"stingray auth login --url {profile.url} --bot-user-id N"
        )
    return profile.bot_user_id


def _summarize(change, payload: dict, result) -> None:
    blocks = payload["code_blocks"]
    lines = sum(b["line_end"] - b["line_start"] + 1 for b in blocks)
    files = sorted({b["filename"] for b in blocks})
    print(f"\n{payload['title']}")
    print(f"  range     {change.description}")
    print(f"  priority  {payload['priority']}")
    print(f"  tags      {', '.join(payload['tags']) or '—'}")
    print(f"  assignee  {payload.get('assigned_to', '—')}")
    print(f"  blocks    {len(blocks)} across {len(files)} file(s), {lines} lines")
    for block in blocks[:10]:
        print(f"            {block['filename']}:{block['line_start']}-{block['line_end']}")
    if len(blocks) > 10:
        print(f"            … and {len(blocks) - 10} more")
    if result.skipped:
        print(f"  skipped   {', '.join(result.skipped[:5])}"
              + (f" (+{len(result.skipped) - 5})" if len(result.skipped) > 5 else ""))
    print()
