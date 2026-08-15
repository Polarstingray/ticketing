"""``stingray auth`` — manage stored credentials."""
from __future__ import annotations

import getpass
import sys

import requests

from stingray_cli.config import (
    ConfigError,
    config_path,
    delete_profile,
    list_profiles,
    load_profile,
    save_profile,
)
from stingray_client.api import StingrayClient


def add_parser(sub, add_connection_flags) -> None:
    parser = sub.add_parser("auth", help="log in / inspect / log out")
    inner = parser.add_subparsers(dest="auth_command", required=True)

    login = inner.add_parser("login", help="store credentials for an instance")
    login.add_argument("--url", metavar="URL", help="Stingray base URL")
    login.add_argument("--profile", metavar="NAME", default="default",
                       help="profile name to write (default: default)")
    login.add_argument("--api-key", metavar="KEY",
                       help="API key; omit to be prompted (or use --stdin)")
    login.add_argument("--stdin", action="store_true",
                       help="read the API key from stdin (for scripts)")
    login.add_argument("--bot-user-id", type=int, metavar="ID",
                       help="resolver bot user id, the default target for "
                            "`review --assign-bot`. The API can't be queried for "
                            "it: listing users is admin-only.")
    login.add_argument("--make-default", action="store_true",
                       help="also make this the default profile")
    login.set_defaults(func=cmd_login)

    status = inner.add_parser("status", help="show the active profile")
    status.add_argument("--profile", metavar="NAME")
    status.set_defaults(func=cmd_status)

    logout = inner.add_parser("logout", help="forget a stored profile")
    logout.add_argument("--profile", metavar="NAME", help="profile to remove")
    logout.set_defaults(func=cmd_logout)


def cmd_login(args) -> int:
    url = (args.url or "").strip().rstrip("/")
    if not url:
        raise ConfigError("--url is required (e.g. --url http://localhost:3000)")

    if args.api_key:
        key = args.api_key.strip()
    elif args.stdin or not sys.stdin.isatty():
        key = sys.stdin.readline().strip()
    else:
        key = getpass.getpass(f"API key for {url}: ").strip()
    if not key:
        raise ConfigError("no API key provided")

    # Validate before storing: a typo'd key that only fails on first real use is
    # much harder to diagnose than one rejected here.
    client = StingrayClient(url, key)
    try:
        me = client.whoami()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise ConfigError(f"the API rejected that key ({status})") from exc
    except requests.RequestException as exc:
        raise ConfigError(f"could not reach {url}: {exc}") from exc

    scopes = _scopes_for(client, me, key)
    path = save_profile(
        args.profile,
        {
            "url": url,
            "api_key": key,
            "bot_user_id": args.bot_user_id,
            "user_id": me.get("id"),
            "username": me.get("username", ""),
            "scopes": scopes,
        },
        make_default=True if args.make_default else None,
    )

    print(f"logged in to {url} as {me.get('username')} (profile: {args.profile})")
    print(f"credentials written to {path} (mode 600)")
    if "cli" not in scopes:
        print(
            "\nnote: this key has no 'cli' scope, so it cannot set repo: tags — "
            "`stingray review` will need --no-repo, and the resolver won't be able "
            "to check out the repo to apply fixes.\n"
            "An admin can mint a scoped key from Profile -> API keys.",
            file=sys.stderr,
        )
    if args.bot_user_id is None:
        print("note: no --bot-user-id stored; `review --assign-bot` will need it.",
              file=sys.stderr)
    return 0


def _scopes_for(client: StingrayClient, me: dict, raw_key: str) -> list[str]:
    """The scopes on the key we just authenticated with, if we can see them.

    Best-effort: key listing is self-or-admin, and we match on the stored prefix
    since the plaintext is never returned again.
    """
    user_id = me.get("id")
    if not user_id:
        return []
    try:
        keys = client._request("GET", f"/users/{user_id}/api-keys").json()
    except Exception:
        return []
    prefix = raw_key[:11]
    for entry in keys:
        if entry.get("key_prefix") == prefix:
            return list(entry.get("scopes") or [])
    return []


def cmd_status(args) -> int:
    profiles, default = list_profiles()
    try:
        profile = load_profile(args.profile)
    except ConfigError as exc:
        print(f"not authenticated: {exc}", file=sys.stderr)
        return 1

    print(f"config    {config_path()}")
    print(f"profile   {profile.name}" + ("  (default)" if profile.name == default else ""))
    print(f"url       {profile.url}")
    print(f"key       {profile.key_display}")
    print(f"scopes    {', '.join(profile.scopes) if profile.scopes else '—'}")
    print(f"user      {profile.username or '?'}"
          + (f" (id {profile.user_id})" if profile.user_id else ""))
    print(f"bot id    {profile.bot_user_id if profile.bot_user_id is not None else '—'}")

    import os
    overrides = [v for v in ("STINGRAY_URL", "STINGRAY_API_KEY") if os.environ.get(v)]
    if overrides:
        print(f"\nnote: {', '.join(overrides)} set in the environment; it overrides the profile.")
    if len(profiles) > 1:
        print(f"\nother profiles: {', '.join(sorted(p for p in profiles if p != profile.name))}")
    return 0


def cmd_logout(args) -> int:
    _, default = list_profiles()
    name = args.profile or default
    if not name:
        print("nothing to log out of", file=sys.stderr)
        return 1
    if not delete_profile(name):
        print(f"no profile named {name!r}", file=sys.stderr)
        return 1
    print(f"removed profile {name!r} from {config_path()}")
    print("note: the key still exists server-side — revoke it from Profile -> API keys.")
    return 0
