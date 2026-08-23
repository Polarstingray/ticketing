"""``stingray explore`` — file one code-review ticket per feature in a codebase.

Where `review` files a ticket about a *change*, `explore` files tickets about what
is already there: a local agent carves the repo into features and each one becomes
a code-review ticket. With `--teach` the descriptions are written for a student
learning the codebase rather than a reviewer who already knows it.
"""
from __future__ import annotations

import json
import sys

from stingray_cli import explore, gitctx
from stingray_cli.agent import AgentError
from stingray_cli.agent import run as run_agent
from stingray_cli.common import check_tags, client_from, confirm, post_ticket, profile_from
from stingray_cli.config import ConfigError
from stingray_client.tickets import PRIORITIES, build_payload


def add_parser(sub, add_connection_flags) -> None:
    parser = sub.add_parser(
        "explore",
        help="file a code-review ticket per feature in this codebase",
        description=(
            "Walks the repository with a local agent, groups it into significant "
            "features, and files one code_review ticket per feature — a reading "
            "guide for a codebase you need to understand. --teach writes the "
            "descriptions as a mentor teaching a student rather than as notes to a "
            "reviewer."
        ),
    )
    parser.add_argument("--feature", metavar="NAME",
                        help="only this feature (default: every significant one)")
    parser.add_argument("--teach", action="store_true",
                        help="write descriptions to teach the codebase, not just describe it")
    parser.add_argument("--priority", default="medium", choices=PRIORITIES,
                        help="fallback priority when the agent suggests none")
    parser.add_argument("--tag", action="append", metavar="TAG", default=[],
                        help="tag every filed ticket (repeatable)")
    parser.add_argument("--assign", type=int, metavar="USER_ID", help="assign to this user id")
    parser.add_argument("--assign-bot", action="store_true",
                        help="assign to the profile's bot_user_id")
    parser.add_argument("--max-features", type=int, default=10, metavar="N",
                        help="cap on tickets filed (default: 10)")
    parser.add_argument("--max-block-lines", type=int, default=400, metavar="N")
    parser.add_argument("--agent", metavar="NAME",
                        help="agent to explore with (claude|opencode); defaults to the "
                             "profile's [describe] agent, shared with `review --describe`")
    parser.add_argument("--timeout", type=int, default=None, metavar="SECONDS",
                        help=f"agent timeout (default: {explore.DEFAULT_TIMEOUT})")
    parser.add_argument("-C", "--root", default=".", help="run as if from this directory")
    parser.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the payloads, don't POST")
    add_connection_flags(parser)
    parser.set_defaults(func=cmd_explore)


def cmd_explore(args) -> int:
    tags = list(args.tag or [])
    # Fail on an unsettable tag before spending several minutes in an agent.
    check_tags(tags)

    if args.max_features < 1:
        print("error: --max-features must be at least 1", file=sys.stderr)
        return 1

    root = gitctx.repo_root(args.root)
    files = explore.list_repo_files(root)
    if not files:
        print(f"error: no reviewable tracked files in {root}", file=sys.stderr)
        return 1

    change = gitctx.resolve_range(root, None, include_worktree=False)

    # Resolved once, before the agent runs: --assign-bot without a stored bot id is a
    # config mistake, and reporting it after a ten-minute discovery run (or letting it
    # escape mid-loop as a traceback under --dry-run) is the worst time to say so.
    try:
        assign = _assignee(args)
    except ConfigError as exc:
        if not args.dry_run:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        # --dry-run POSTs nothing, so an unresolvable assignee must not stop it.
        print(f"warning: {exc}\nwarning: --dry-run continues with no assignee",
              file=sys.stderr)
        assign = None

    # Unlike `review`, nothing uncommitted is ever quoted: blocks are read at
    # head_sha. But the agent reads the worktree, so on a dirty tree it can describe
    # code the ticket does not contain. Say so rather than let the difference surface
    # as confusing review findings.
    if change.head_sha and gitctx.git(root, "status", "--porcelain").strip():
        print(f"warning: {root.name} has uncommitted changes. The agent reads the "
              f"working tree, but tickets quote commit {change.head_sha[:12]}, so a "
              f"description may not match the code it links to. Commit first for a "
              f"fully faithful map.", file=sys.stderr)

    settings = dict(getattr(_profile_or_none(args), "describe", None) or {})
    try:
        output = run_agent(
            explore.build_discovery_prompt(root, files, args.feature, args.teach),
            root,
            agent=args.agent or settings.get("agent") or None,
            model=settings.get("model") or None,
            timeout=args.timeout or int(settings.get("timeout", explore.DEFAULT_TIMEOUT)),
        )
    except AgentError as exc:
        print(f"error: feature discovery failed ({exc})", file=sys.stderr)
        return 1

    features = explore.parse_feature_tickets(output)
    if not features:
        what = f"feature {args.feature!r}" if args.feature else "any features"
        print(f"error: the agent did not identify {what} in {root.name} "
              f"(no usable JSON in its output)", file=sys.stderr)
        return 1

    if len(features) > args.max_features:
        print(f"note: {len(features)} features found, filing the first "
              f"{args.max_features} (raise --max-features)", file=sys.stderr)
        features = features[:args.max_features]

    payloads: list[dict] = []
    for feature in features:
        result = explore.build_code_blocks_for_feature(
            root, feature["files"], change.head_sha or None,
            max_block_lines=args.max_block_lines,
        )
        if not result.blocks:
            print(f"warning: skipping feature {feature['name']!r} — none of its files "
                  f"could be read ({', '.join(feature['files'][:3])})", file=sys.stderr)
            continue
        if result.skipped:
            # Partial hallucination: the description still talks about these files, so
            # the ticket quotes less than it claims. Name them instead of dropping
            # them silently — the mismatch is otherwise undiagnosable from the ticket.
            print(f"warning: feature {feature['name']!r} names files that could not be "
                  f"read and are not quoted: {', '.join(result.skipped)}",
                  file=sys.stderr)
        payloads.append(build_payload(
            type="code_review",
            title=f"Review: {feature['title']}",
            description=feature["description"],
            priority=feature["priority"] or args.priority,
            tags=list(tags),
            code_blocks=result.blocks,
            root=root,
            rev=change.head_sha,
            branch=change.branch,
            assign=assign,
        ))

    if not payloads:
        print("error: every discovered feature pointed at files that could not be "
              "read; nothing to file", file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps(payloads, indent=2))
        return 0

    _summarize(root, payloads, teach=args.teach)
    if not confirm(f"File {len(payloads)} ticket(s)?", assume_yes=args.yes):
        print("aborted", file=sys.stderr)
        return 1

    client, profile = client_from(args)
    failures = 0
    for payload in payloads:
        failures += post_ticket(client, profile, payload)
    if failures:
        print(f"error: {failures} of {len(payloads)} ticket(s) could not be filed",
              file=sys.stderr)
        return 1
    return 0


def _profile_or_none(args):
    """The profile, if one resolves. Discovery is local, so --dry-run must still
    work before any credentials are stored."""
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


def _summarize(root, payloads: list[dict], *, teach: bool) -> None:
    mode = "teach" if teach else "review"
    print(f"\n{len(payloads)} feature ticket(s) from {root.name} ({mode} mode)")
    for payload in payloads:
        files = sorted({b["filename"] for b in payload["code_blocks"]})
        print(f"  {payload['priority']:<8} {payload['title']}")
        print(f"           {', '.join(files)}")
    print()
