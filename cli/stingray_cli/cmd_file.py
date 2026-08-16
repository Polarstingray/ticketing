"""``stingray file`` — file an arbitrary ticket. The validated replacement for curl.

Mirrors ``resolver/file_ticket.py``'s surface minus ``--parent``: delegation is a
resolver-to-resolver concern, and ``parent:`` is a reserved tag the CLI's key
cannot set anyway.
"""
from __future__ import annotations

import json

from stingray_cli.common import check_tags, client_from, post_ticket
from stingray_client.tickets import PRIORITIES, TYPES, build_payload


def add_parser(sub, add_connection_flags) -> None:
    parser = sub.add_parser("file", help="file a ticket (validated, no curl)")
    parser.add_argument("--type", required=True, choices=TYPES)
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--priority", default="medium", choices=PRIORITIES)
    parser.add_argument("--tag", action="append", metavar="TAG", help="repeatable")
    parser.add_argument("--assign", type=int, metavar="USER_ID")
    parser.add_argument("--repo", metavar="NAME",
                        help="target repo (repo:<NAME>); defaults to this checkout")
    parser.add_argument("--no-repo", action="store_true")
    parser.add_argument("--code-block", action="append", dest="code_block",
                        metavar="PATH:LANG:START-END",
                        help="repeatable; reads the lines from disk (code_review only)")
    parser.add_argument("-C", "--root", default=".",
                        help="base dir for --code-block paths (default: cwd)")
    parser.add_argument("--dry-run", action="store_true")
    add_connection_flags(parser)
    parser.set_defaults(func=cmd_file)


def cmd_file(args) -> int:
    tags = list(args.tag or [])
    check_tags(tags)

    payload = build_payload(
        type=args.type,
        title=args.title,
        description=args.description,
        priority=args.priority,
        tags=tags,
        code_block_specs=args.code_block or [],
        root=args.root,
        repo=args.repo,
        no_repo=args.no_repo,
        assign=args.assign,
    )

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    client, profile = client_from(args)
    return post_ticket(client, profile, payload)
