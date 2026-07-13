#!/usr/bin/env python3
"""``resolver`` — one-stop CLI for creating and managing Stingray resolvers.

The resolver is several small scripts (``resolve_tickets.py`` sweeps, ``logs.py``
tails logs, ``file_ticket.py`` files tickets) plus per-identity ``.env`` files and
a hand-built ``RESOLVER_WORKERS`` roster. This wraps all of that behind one front
door so an operator can stand up and run bots from a single place:

    resolver bot create open --desc "cheap mechanical fixes"
    resolver bot list
    resolver roster
    resolver run --env .env.open --dry-run
    resolver stats --ticket 42

Admin operations (``bot create``/``bot list``) need an admin API key
(``--admin-key`` or ``$STINGRAY_ADMIN_KEY``) since the resolver's own key is a
least-privilege member. Everything else uses a resolver ``.env`` identity.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ENV_EXAMPLE = HERE / ".env.example"
# A resolver identity lives in `.env` or `.env.<name>`; the example is not one.
ENV_GLOB = ".env*"
DESC_KEY = "RESOLVER_BOT_DESC"  # non-config hint the roster reads back


# --- helpers -----------------------------------------------------------------

def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE .env file into a dict (no environ mutation, no interp)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _identity_files() -> list[Path]:
    """Every resolver identity file in the resolver dir (.env, .env.<name>),
    excluding the template .env.example."""
    return sorted(
        p for p in HERE.glob(ENV_GLOB)
        if p.is_file() and p.name != ".env.example"
    )


def _identity_name(path: Path) -> str:
    """`.env` -> 'default'; `.env.open` -> 'open'."""
    return "default" if path.name == ".env" else path.name[len(".env."):]


def _resolve_admin(args) -> tuple[str, str]:
    """(base_url, admin_key) for admin API calls, from flags or environment.

    The URL also falls back to STINGRAY_URL in the local `.env` so an operator who
    already configured a resolver doesn't have to repeat it."""
    url = (args.url or os.environ.get("STINGRAY_URL", "")).strip()
    if not url:
        url = _read_env_file(HERE / ".env").get("STINGRAY_URL", "").strip()
    key = (args.admin_key or os.environ.get("STINGRAY_ADMIN_KEY", "")).strip()
    if not url:
        sys.exit("error: no Stingray URL (pass --url or set STINGRAY_URL)")
    if not key:
        sys.exit("error: no admin API key (pass --admin-key or set STINGRAY_ADMIN_KEY)")
    return url.rstrip("/"), key


def _write_identity(name: str, *, url: str, api_key: str, user_id: int,
                    desc: str, projects_root: str, force: bool) -> Path:
    """Write `.env.<name>` from the example template with this bot's values."""
    dest = HERE / f".env.{name}"
    if dest.exists() and not force:
        sys.exit(f"error: {dest.name} already exists (pass --force to overwrite)")
    overrides = {
        "STINGRAY_URL": url,
        "STINGRAY_API_KEY": api_key,
        "RESOLVER_BOT_USER_ID": str(user_id),
    }
    if projects_root:
        overrides["PROJECTS_ROOT"] = projects_root
    lines: list[str] = []
    seen: set[str] = set()
    for raw in ENV_EXAMPLE.read_text().splitlines():
        stripped = raw.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.partition("=")[0].strip()
            if key in overrides:
                lines.append(f"{key}={overrides[key]}")
                seen.add(key)
                continue
        lines.append(raw)
    # Append any overrides the template didn't carry, plus the roster description.
    for key in overrides:
        if key not in seen:
            lines.append(f"{key}={overrides[key]}")
    if desc:
        lines.append(f"{DESC_KEY}={desc}")
    dest.write_text("\n".join(lines) + "\n")
    return dest


# --- commands ----------------------------------------------------------------

def cmd_bot_create(args) -> int:
    url, admin_key = _resolve_admin(args)
    body = {"username": args.username}
    if args.display_name:
        body["display_name"] = args.display_name
    if args.email:
        body["email"] = args.email
    try:
        resp = requests.post(f"{url}/users/resolver-bot", json=body,
                             headers={"X-API-Key": admin_key}, timeout=30)
    except requests.RequestException as exc:
        sys.exit(f"error: could not reach Stingray: {exc}")
    if resp.status_code != 201:
        sys.exit(f"error: creating bot failed ({resp.status_code})\n{resp.text}")
    data = resp.json()
    print(f"created resolver bot '{data['username']}' (id={data['user_id']})")
    name = args.name or args.username
    if args.no_env_file:
        print(f"API key (shown once): {data['api_key']}")
        return 0
    dest = _write_identity(
        name, url=url, api_key=data["api_key"], user_id=data["user_id"],
        desc=args.desc or "", projects_root=args.projects_root or "", force=args.force,
    )
    print(f"wrote {dest.relative_to(HERE.parent)} — edit PROJECTS_ROOT/models as needed")
    print(f"run it with:  resolver run --env {dest.name}")
    return 0


def cmd_bot_list(args) -> int:
    url, admin_key = _resolve_admin(args)
    try:
        resp = requests.get(f"{url}/users", headers={"X-API-Key": admin_key}, timeout=30)
    except requests.RequestException as exc:
        sys.exit(f"error: could not reach Stingray: {exc}")
    if resp.status_code != 200:
        sys.exit(f"error: listing users failed ({resp.status_code})\n{resp.text}")
    bots = [u for u in resp.json() if u.get("is_resolver_bot")]
    if not bots:
        print("no resolver bots yet — create one with `resolver bot create <username>`")
        return 0
    # Map each bot id to a local identity file (by RESOLVER_BOT_USER_ID), if any.
    env_by_id: dict[str, str] = {}
    desc_by_id: dict[str, str] = {}
    for path in _identity_files():
        env = _read_env_file(path)
        uid = env.get("RESOLVER_BOT_USER_ID", "")
        if uid:
            env_by_id[uid] = path.name
            if env.get(DESC_KEY):
                desc_by_id[uid] = env[DESC_KEY]
    print(f"{'ID':<5} {'USERNAME':<20} {'ENV FILE':<16} DESC")
    for b in bots:
        uid = str(b["id"])
        print(f"{uid:<5} {b['username']:<20} {env_by_id.get(uid, '—'):<16} "
              f"{desc_by_id.get(uid, '')}")
    return 0


def cmd_roster(args) -> int:
    """Assemble RESOLVER_WORKERS (id:name:desc;...) from local identity files."""
    parts: list[str] = []
    for path in _identity_files():
        env = _read_env_file(path)
        uid = env.get("RESOLVER_BOT_USER_ID", "").strip()
        if not uid:
            continue
        name = _identity_name(path)
        desc = env.get(DESC_KEY, "").replace(";", ",").replace(":", " ")
        parts.append(f"{uid}:{name}:{desc}" if desc else f"{uid}:{name}:")
    if not parts:
        print("no identities with RESOLVER_BOT_USER_ID found in resolver/.env*")
        return 0
    print("RESOLVER_WORKERS=" + ";".join(parts))
    return 0


def _run_script(script: str, rest: list[str], env_file: str | None) -> int:
    env = dict(os.environ)
    if env_file:
        env["RESOLVER_ENV_FILE"] = env_file
    return subprocess.call([sys.executable, str(HERE / script), *rest], env=env)


def cmd_run(args) -> int:
    return _run_script("resolve_tickets.py", args.rest, args.env)


def cmd_logs(args) -> int:
    return _run_script("logs.py", args.rest, args.env)


def cmd_file(args) -> int:
    return _run_script("file_ticket.py", args.rest, args.env)


def cmd_stats(args) -> int:
    # Imported lazily so the admin/run paths don't pay Config.load's validation.
    if args.env:
        os.environ["RESOLVER_ENV_FILE"] = args.env
    from config import Config
    from stingray import StingrayClient

    cfg = Config.load()
    client = StingrayClient(cfg.stingray_url, cfg.api_key,
                            max_retries=cfg.stingray_max_retries)
    if args.ticket is not None:
        roll = client.cost_rollup(args.ticket)
        t = roll["total"]
        print(f"ticket #{roll['ticket_id']}: ${t['cost_usd']:.4f} over {t['run_count']} run(s), "
              f"{t['input_tokens']}+{t['output_tokens']} tok (own + {len(roll['children'])} child)")
        for c in roll["children"]:
            ct = c["totals"]
            print(f"  └ #{c['ticket_id']} {c['title'][:40]:<40} ${ct['cost_usd']:.4f}")
        return 0

    # Aggregate every ticket this bot owns (its key only sees those).
    by_phase: dict[str, dict] = {}
    total_cost = 0.0
    total_runs = 0
    for ticket in client.iter_tickets(assigned_to=cfg.bot_user_id):
        for run in client.list_agent_runs(ticket["id"]):
            ph = run["phase"]
            agg = by_phase.setdefault(ph, {"cost": 0.0, "runs": 0, "in": 0, "out": 0})
            agg["cost"] += run["cost_usd"]
            agg["runs"] += 1
            agg["in"] += run["input_tokens"]
            agg["out"] += run["output_tokens"]
            total_cost += run["cost_usd"]
            total_runs += 1
    if not total_runs:
        print(f"no agent runs recorded for bot user {cfg.bot_user_id}")
        return 0
    print(f"{'PHASE':<16} {'RUNS':>5} {'IN TOK':>10} {'OUT TOK':>10} {'COST':>10}")
    for ph, a in sorted(by_phase.items()):
        print(f"{ph:<16} {a['runs']:>5} {a['in']:>10} {a['out']:>10} ${a['cost']:>9.4f}")
    print(f"{'TOTAL':<16} {total_runs:>5} {'':>10} {'':>10} ${total_cost:>9.4f}")
    return 0


# --- parser ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="resolver", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    bot = sub.add_parser("bot", help="create / list resolver bot users")
    botsub = bot.add_subparsers(dest="bot_command", required=True)

    create = botsub.add_parser("create", help="provision a resolver bot + .env file")
    create.add_argument("username", help="bot username (e.g. open-bot)")
    create.add_argument("--name", help="identity name for the .env.<name> file "
                        "(default: the username)")
    create.add_argument("--display-name", help="display name (default: username)")
    create.add_argument("--email", help="email (default: <username>@localhost)")
    create.add_argument("--desc", help="one-line role blurb used by `resolver roster`")
    create.add_argument("--projects-root", help="PROJECTS_ROOT for the new .env file")
    create.add_argument("--url", help="Stingray base URL (default: $STINGRAY_URL or .env)")
    create.add_argument("--admin-key", help="admin API key (default: $STINGRAY_ADMIN_KEY)")
    create.add_argument("--no-env-file", action="store_true",
                        help="just print the key; don't write a .env file")
    create.add_argument("--force", action="store_true", help="overwrite an existing .env file")
    create.set_defaults(func=cmd_bot_create)

    blist = botsub.add_parser("list", help="list resolver bots and their .env files")
    blist.add_argument("--url", help="Stingray base URL (default: $STINGRAY_URL or .env)")
    blist.add_argument("--admin-key", help="admin API key (default: $STINGRAY_ADMIN_KEY)")
    blist.set_defaults(func=cmd_bot_list)

    roster = sub.add_parser("roster", help="build a RESOLVER_WORKERS string from .env files")
    roster.set_defaults(func=cmd_roster)

    run = sub.add_parser("run", help="run a sweep (wraps resolve_tickets.py)")
    run.add_argument("--env", help="resolver .env file to use (sets RESOLVER_ENV_FILE)")
    run.add_argument("rest", nargs=argparse.REMAINDER,
                     help="args for resolve_tickets.py (e.g. --ticket 5 --dry-run)")
    run.set_defaults(func=cmd_run)

    stats = sub.add_parser("stats", help="show token usage + cost from agent runs")
    stats.add_argument("--ticket", type=int, help="one ticket (incl. delegated children)")
    stats.add_argument("--env", help="resolver .env file to use (sets RESOLVER_ENV_FILE)")
    stats.set_defaults(func=cmd_stats)

    logs = sub.add_parser("logs", help="view per-ticket logs (wraps logs.py)")
    logs.add_argument("--env", help="resolver .env file to use (sets RESOLVER_ENV_FILE)")
    logs.add_argument("rest", nargs=argparse.REMAINDER, help="args for logs.py")
    logs.set_defaults(func=cmd_logs)

    filecmd = sub.add_parser("file", help="file a ticket (wraps file_ticket.py)")
    filecmd.add_argument("--env", help="resolver .env file to use (sets RESOLVER_ENV_FILE)")
    filecmd.add_argument("rest", nargs=argparse.REMAINDER, help="args for file_ticket.py")
    filecmd.set_defaults(func=cmd_file)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
