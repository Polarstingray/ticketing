"""Helpers shared by the subcommands: client construction, confirmation, output."""
from __future__ import annotations

import sys

import requests

from stingray_cli.config import ConfigError, Profile, load_profile
from stingray_client.api import NotJsonError, StingrayClient
from stingray_client.tickets import is_reserved_tag

# Reserved tags a `cli`-scoped key is allowed to set (mirrors the server's
# control_tags.SCOPE_TAG_PREFIXES). Everything else reserved is refused here so
# the user finds out before we spend time diffing or calling an agent.
CLI_SETTABLE_PREFIXES = ("repo:",)


def profile_from(args) -> Profile:
    return load_profile(
        getattr(args, "profile", None),
        url=getattr(args, "url", None),
        api_key=getattr(args, "api_key", None),
    )


def client_from(args) -> tuple[StingrayClient, Profile]:
    profile = profile_from(args)
    return StingrayClient(profile.url, profile.api_key), profile


def check_tags(tags: list[str]) -> None:
    """Reject reserved tags this client can't set, before doing real work."""
    bad = [
        t for t in tags
        if is_reserved_tag(t) and not t.startswith(CLI_SETTABLE_PREFIXES)
    ]
    if bad:
        raise ConfigError(
            f"reserved tags cannot be set from the CLI: {', '.join(sorted(bad))}. "
            "They are managed by the resolver's automation."
        )


def confirm(prompt: str, *, assume_yes: bool = False) -> bool:
    """Ask before doing something that leaves a trace on the server.

    Never blocks on a non-interactive stdin: a script that forgot `--yes` should
    fail with a clear message rather than hang in CI.
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise ConfigError("not a terminal: pass --yes to confirm non-interactively")
    reply = input(f"{prompt} [y/N] ").strip().lower()
    return reply in ("y", "yes")


def post_ticket(client: StingrayClient, profile: Profile, payload: dict) -> int:
    """POST a ticket, print where it landed, and translate API errors."""
    try:
        ticket = client.create_ticket(**payload)
    except requests.HTTPError as exc:
        resp = exc.response
        detail = resp.text if resp is not None else str(exc)
        status = resp.status_code if resp is not None else "?"
        print(f"error: filing ticket failed ({status})\n{detail}", file=sys.stderr)
        if status == 422 and any(t.startswith("repo:") for t in payload.get("tags", [])):
            print(
                "\nhint: setting a repo: tag needs an API key with the 'cli' scope. "
                "An admin can mint one from Profile -> API keys.",
                file=sys.stderr,
            )
        return 1
    except NotJsonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"error: could not reach Stingray at {profile.url}: {exc}", file=sys.stderr)
        return 1

    print(f"created ticket #{ticket['id']}: {ticket['title']}")
    print(f"{profile.web_url}/tickets/{ticket['id']}")
    return 0
