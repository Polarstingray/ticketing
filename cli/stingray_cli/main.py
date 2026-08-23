"""``stingray`` — file Stingray Tickets from git, and scaffold new projects.

Subcommands:
  auth      log in / inspect / log out of a Stingray instance
  review    file a code-review ticket from a git range
  explore   file a code-review ticket per feature in an existing codebase
  file      file an arbitrary ticket (the validated replacement for curl)
  scaffold  generate a project outline plus a ticket per stub
"""
from __future__ import annotations

import argparse
import sys

from stingray_cli import cmd_auth, cmd_explore, cmd_file, cmd_review, cmd_scaffold
from stingray_cli.config import ConfigError
from stingray_cli.gitctx import GitError


def _add_connection_flags(parser: argparse.ArgumentParser) -> None:
    """Flags every network subcommand shares (they beat env and the config file)."""
    parser.add_argument("--profile", metavar="NAME", help="config profile to use")
    parser.add_argument("--url", metavar="URL", help="Stingray base URL (overrides config)")
    parser.add_argument("--api-key", metavar="KEY", help="API key (overrides config)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stingray",
        description="File Stingray Tickets from git, and scaffold projects with a backlog.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cmd_auth.add_parser(sub, _add_connection_flags)
    cmd_review.add_parser(sub, _add_connection_flags)
    cmd_explore.add_parser(sub, _add_connection_flags)
    cmd_file.add_parser(sub, _add_connection_flags)
    cmd_scaffold.add_parser(sub, _add_connection_flags)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, GitError) as exc:
        # Expected, actionable failures: no traceback, just the message.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
