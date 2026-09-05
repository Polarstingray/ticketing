"""``stingray station`` — the resolvers running on this host.

One front door for what used to take four: the bot on the server, the API key,
the ``.env.<name>`` in a checkout, and the systemd unit pair. The station
records which identities this host means to run and derives everything else
live, so the answer to "is it up, on what revision, and when did it last work"
is one command instead of four.
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import requests

from stingray_cli import config as cfgstore
from stingray_cli.common import profile_from
from stingray_cli.config import ConfigError
from stingray_cli.station import identity, units
from stingray_cli.station.inventory import (
    DEFAULT_UNIT_PREFIX,
    Resolver,
    Station,
    load_station,
    save_station,
    stations_path,
    validate_name,
)
from stingray_cli.station.status import ResolverStatus, collect
from stingray_client.api import StingrayClient


def add_parser(sub, add_connection_flags) -> None:
    parser = sub.add_parser(
        "station",
        help="manage the resolvers running on this host",
        description=__doc__.splitlines()[1] if __doc__ else None,
    )
    inner = parser.add_subparsers(dest="station_command", required=True)

    p_init = inner.add_parser("init", help="adopt this host and everything already on it")
    p_init.add_argument("--name", help="station name (default: this host's name)")
    p_init.add_argument("--checkout", action="append", metavar="DIR", default=None,
                        help="a ticketing checkout to scan (repeatable; default: "
                             "whatever the installed unit templates point at)")
    p_init.add_argument("--profile", metavar="NAME",
                        help="fallback profile for identities whose URL matches none")
    p_init.set_defaults(func=cmd_init)

    p_adopt = inner.add_parser("adopt", help="manage an identity that already exists")
    p_adopt.add_argument("name")
    p_adopt.add_argument("--checkout", required=True, metavar="DIR")
    p_adopt.add_argument("--env-file", metavar="FILE",
                         help="identity file (default: .env.<instance>)")
    p_adopt.add_argument("--instance", metavar="NAME",
                         help="systemd instance / .env suffix, when it differs from "
                              "the handle (default: the handle up to any '@')")
    p_adopt.add_argument("--unit-prefix", default=None,
                         help=f"systemd unit family (default: {DEFAULT_UNIT_PREFIX})")
    p_adopt.add_argument("--profile", metavar="NAME",
                         help="profile naming the server (default: matched by URL)")
    p_adopt.set_defaults(func=cmd_adopt)

    p_enroll = inner.add_parser(
        "enroll",
        help="redeem an enrolment token into a working resolver",
        description="Redeem a token an admin minted in the web app. The station "
                    "never needs an admin key: the token is a one-shot capability "
                    "for exactly one bot.")
    p_enroll.add_argument("token")
    p_enroll.add_argument("--name", help="local name (default: the bot's username)")
    p_enroll.add_argument("--checkout", required=True, metavar="DIR")
    p_enroll.add_argument("--unit-prefix", default=None,
                          help=f"systemd unit family (default: {DEFAULT_UNIT_PREFIX})")
    p_enroll.add_argument("--projects-root", default="", metavar="DIR",
                          help="PROJECTS_ROOT for the new identity")
    p_enroll.add_argument("--start", action="store_true",
                          help="install units and start it once enrolled")
    p_enroll.add_argument("--force", action="store_true",
                          help="overwrite an existing identity file")
    add_connection_flags(p_enroll)
    p_enroll.set_defaults(func=cmd_enroll)

    p_forget = inner.add_parser("forget", help="stop managing an identity (changes nothing on disk)")
    p_forget.add_argument("name")
    p_forget.set_defaults(func=cmd_forget)

    p_ls = inner.add_parser("ls", help="one line per resolver on this station")
    p_ls.set_defaults(func=cmd_ls)

    p_status = inner.add_parser("status", help="everything known about one resolver")
    p_status.add_argument("name", nargs="?")
    p_status.set_defaults(func=cmd_status)

    for verb, helptext in (("start", "enable and start the timer and listener"),
                           ("stop", "stop and disable both"),
                           ("restart", "restart the listener")):
        p = inner.add_parser(verb, help=helptext)
        p.add_argument("name")
        p.set_defaults(func={"start": cmd_start, "stop": cmd_stop,
                             "restart": cmd_restart}[verb])

    p_sweep = inner.add_parser("sweep", help="run one sweep now, as the listener's poke does")
    p_sweep.add_argument("name")
    p_sweep.set_defaults(func=cmd_sweep)

    p_logs = inner.add_parser("logs", help="tail a resolver's log")
    p_logs.add_argument("name")
    p_logs.add_argument("-f", "--follow", action="store_true")
    p_logs.add_argument("-n", "--lines", type=int, default=40)
    which = p_logs.add_mutually_exclusive_group()
    which.add_argument("--sweep", action="store_const", const="sweep", dest="which")
    which.add_argument("--listener", action="store_const", const="listener", dest="which")
    p_logs.set_defaults(func=cmd_logs, which="sweep")


# --- helpers ----------------------------------------------------------------

def _profile_for_url(url: str, fallback: str | None) -> str:
    """Which configured profile names the server this identity talks to.

    Matched by URL rather than assumed, because this host talks to two servers
    and the identity file is the only thing that knows which one it means.

    A URL that matches nothing returns "" even when a fallback was offered.
    Falling back there would quietly file a localhost resolver under the profile
    for a different server — precisely the mislabelling this whole tool exists
    to prevent. The fallback applies only when the identity names no URL at all.
    """
    profiles, default = cfgstore.list_profiles()
    target = (url or "").rstrip("/")
    if target:
        for name, stanza in profiles.items():
            if str(stanza.get("url", "")).rstrip("/") == target:
                return name
        return ""
    return fallback or default or ""


def _server_url(args) -> str:
    """Where to redeem. A profile is not required — that is the point.

    A host enrolling its first resolver has no credentials at all, so `--url`
    has to be enough; a host that already talks to this server can name the
    profile instead of retyping it.
    """
    if getattr(args, "url", None):
        return args.url.rstrip("/")
    try:
        return profile_from(args).url
    except ConfigError:
        raise ConfigError(
            "no --url given and no profile is configured. Point at the server "
            "directly: stingray station enroll TOKEN --url https://tickets.example/api "
            "--checkout DIR"
        ) from None


def _families() -> dict[str, Path]:
    """Installed unit templates, as ``prefix -> resolver directory``.

    Read back out of the units rather than guessed: the prefix is what keeps two
    checkouts on one host from colliding, and the units are the only place that
    mapping is already recorded.
    """
    found: dict[str, Path] = {}
    unit_dir = units.user_unit_dir()
    if not unit_dir.is_dir():
        return found
    for path in sorted(unit_dir.glob("*@.service")):
        if "-listen@" in path.name:
            continue  # derived from its family's prefix
        prefix = path.name[: -len("@.service")]
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("WorkingDirectory="):
                found[prefix] = Path(line.partition("=")[2].strip())
                break
    return found


def _fmt_since(state: units.UnitState) -> str:
    if not state.ok or not state.since:
        return ""
    # systemd prints "Sat 2026-09-05 00:57:11 CDT"; the clock is the useful part.
    parts = state.since.split()
    return parts[2] if len(parts) > 2 else state.since


def _has_units(resolver: Resolver) -> bool:
    """Whether this identity's sweep timer was actually enabled by someone."""
    return units.unit_state(resolver.timer_unit).installed


def _status_rows(station: Station, names: list[str]) -> list[ResolverStatus]:
    return [collect(station.get(n)) for n in names]


# --- commands ---------------------------------------------------------------

def cmd_init(args) -> int:
    station = load_station(required=False)
    station.name = args.name or station.name or socket.gethostname()

    families = _families()
    if args.checkout:
        wanted = {Path(c).expanduser().resolve() for c in args.checkout}
        families = {p: d for p, d in families.items() if d.parent.resolve() in wanted}
        for path in wanted:
            if not any(d.parent.resolve() == path for d in families.values()):
                families.setdefault(DEFAULT_UNIT_PREFIX, path / "resolver")
    if not families:
        raise ConfigError(
            "found no installed unit templates and no --checkout was given. "
            "Point at a checkout: stingray station init --checkout ~/projects/ticketing"
        )

    adopted, skipped = [], []
    for prefix, resolver_dir in sorted(families.items()):
        for env_path in identity.discover(resolver_dir):
            name = identity.identity_name(env_path.name)
            if env_path.name == ".env":
                # A bare `.env` cannot be a systemd instance name. It is usually
                # already reachable through a `.env.<name>` symlink, which is
                # discovered separately — so say so rather than inventing one.
                skipped.append(f"{env_path} (bare .env — symlink it as .env.<name>)")
                continue
            if any(r.instance == name and r.unit_prefix == prefix
                   for r in station.resolvers.values()):
                continue  # already managed
            env = identity.read_env(env_path)
            bot = env.get("RESOLVER_BOT_USER_ID", "")
            url = env.get("STINGRAY_URL", "")
            profile = _profile_for_url(url, args.profile)
            if not profile:
                skipped.append(
                    f"{env_path} (no profile points at {url or '(no URL set)'} — add one "
                    f"with `stingray auth login --profile NAME --url {url or 'URL'}`)")
                continue
            bot_id = int(bot) if bot.isdigit() else None
            if bot_id is not None:
                clash = station.by_bot(profile, bot_id)
                if clash:
                    # One registry row per bot on the server, so two identities
                    # sharing one would overwrite each other's heartbeat and make
                    # both flicker between live and dead. Prefer whichever one is
                    # actually installed in systemd: a stale identity file left
                    # beside a live one is the common case, and picking by
                    # alphabet would adopt the dead one.
                    keep, drop = clash, (name, env_path)
                    if not _has_units(clash) and _has_units(
                            Resolver(handle=name, instance=name, profile=profile,
                                     checkout=resolver_dir.parent,
                                     env_file=env_path.name, bot_user_id=bot_id,
                                     unit_prefix=prefix)):
                        del station.resolvers[clash.handle]
                        adopted[:] = [a for a in adopted
                                      if not a.startswith(f"{clash.handle} ")]
                        keep = None
                    if keep is not None:
                        skipped.append(
                            f"{drop[1]} (bot {bot_id} on {profile} is already "
                            f"{keep.handle!r}, which has units installed — one bot "
                            f"belongs to one resolver)")
                        continue
                    skipped.append(
                        f"{clash.env_path} (bot {bot_id} on {profile} has no units "
                        f"installed; {name!r} does, so that one is managed instead)")
            handle = name if name not in station.resolvers else f"{name}@{profile}"
            if handle in station.resolvers:
                skipped.append(f"{env_path} (handle {handle!r} is already taken)")
                continue
            station.resolvers[handle] = Resolver(
                handle=handle, instance=name, profile=profile,
                checkout=resolver_dir.parent, env_file=env_path.name,
                bot_user_id=bot_id, unit_prefix=prefix,
            )
            label = handle if handle == name else f"{handle} (instance {name})"
            adopted.append(f"{label} (bot {bot or '?'}, {profile}, {prefix}@)")

    path = save_station(station)
    print(f"station '{station.name}' -> {path}")
    for line in adopted:
        print(f"  adopted {line}")
    for line in skipped:
        print(f"  skipped {line}", file=sys.stderr)
    if not adopted:
        print("  (nothing new to adopt)")
    return 0


def cmd_adopt(args) -> int:
    handle = validate_name(args.name, handle=True)
    name = validate_name(args.instance or args.name.split("@", 1)[0])
    station = load_station(required=False)
    if not station.name:
        station.name = socket.gethostname()
    checkout = Path(args.checkout).expanduser().resolve()
    env_file = args.env_file or f".env.{name}"
    prefix = args.unit_prefix or DEFAULT_UNIT_PREFIX

    env_path = checkout / "resolver" / env_file
    if not env_path.is_file():
        raise ConfigError(f"{env_path} does not exist — adopt manages an identity that "
                          f"already has one")
    env = identity.read_env(env_path)
    bot = env.get("RESOLVER_BOT_USER_ID", "")
    profile = _profile_for_url(env.get("STINGRAY_URL", ""), args.profile)
    if not profile:
        raise ConfigError(
            f"no configured profile points at {env.get('STINGRAY_URL', '(unset)')}. "
            "Pass --profile, or add one with: stingray auth login --profile NAME --url URL"
        )

    bot_id = int(bot) if bot.isdigit() else None
    if bot_id is not None:
        clash = station.by_bot(profile, bot_id)
        if clash and clash.handle != handle:
            # The server keeps one registry row per bot (AgentInstance is unique
            # on user_id), so two identities sharing a bot would overwrite each
            # other's heartbeat and make both look intermittently dead.
            raise ConfigError(
                f"bot {bot_id} on profile {profile!r} is already managed as "
                f"{clash.handle!r}. One bot belongs to one resolver."
            )

    station.resolvers[handle] = Resolver(
        handle=handle, instance=name, profile=profile, checkout=checkout,
        env_file=env_file, bot_user_id=bot_id, unit_prefix=prefix,
    )
    save_station(station)
    print(f"adopted {handle} (bot {bot or '?'}, profile {profile}, "
          f"units {prefix}@{name})")
    if not units.templates_installed(prefix):
        print(f"note: the {prefix}@ unit templates are not installed; "
              f"`stingray station start {handle}` will install them.", file=sys.stderr)
    return 0


def cmd_enroll(args) -> int:
    """Token in, running resolver out.

    The four things that have to agree — bot, key, identity file, units — are
    created together here, which is the whole reason this command exists. The
    admin named the bot when they minted the token, so nothing about the
    identity is chosen on this side except where it lives.
    """
    url = _server_url(args)
    checkout = Path(args.checkout).expanduser().resolve()
    resolver_dir = checkout / "resolver"
    if not resolver_dir.is_dir():
        raise ConfigError(f"{resolver_dir} does not exist — --checkout wants a "
                          f"ticketing checkout, not its resolver/ directory")

    station = load_station(required=False)
    if not station.name:
        station.name = socket.gethostname()

    # The token is spent by this call whether or not the rest succeeds, so
    # everything that can be checked is checked before it is handed over.
    client = StingrayClient(url, "")
    try:
        granted = client.redeem_enrollment(args.token, station=station.name)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        if status == 404:
            raise ConfigError(
                "the server rejected that token. Enrolment tokens are single-use "
                "and short-lived — ask an admin to mint a fresh one."
            ) from None
        detail = exc.response.text if exc.response is not None else str(exc)
        raise ConfigError(f"redeeming failed ({status}): {detail}") from None
    except requests.RequestException as exc:
        raise ConfigError(f"could not reach Stingray at {url}: {exc}") from None

    instance = validate_name(args.name or granted["username"])
    handle = instance if instance not in station.resolvers else f"{instance}@{_profile_for_url(url, args.profile) or 'server'}"
    prefix = args.unit_prefix or DEFAULT_UNIT_PREFIX

    env_path = identity.write_identity(
        resolver_dir, instance, url=url, api_key=granted["api_key"],
        user_id=granted["user_id"], projects_root=args.projects_root,
        force=args.force,
    )
    print(f"enrolled {granted['username']} as user {granted['user_id']}")
    print(f"wrote {env_path}")

    profile = _profile_for_url(url, args.profile)
    if not profile:
        # The identity is real and usable; only this host's bookkeeping is
        # missing, so say exactly what closes the gap rather than failing after
        # spending the token.
        print(f"\nnote: no configured profile points at {url}, so this resolver "
              f"is not in the station inventory yet. Finish with:\n"
              f"  stingray auth login --profile NAME --url {url}\n"
              f"  stingray station adopt {instance} --checkout {checkout}"
              + (f" --unit-prefix {prefix}" if prefix != DEFAULT_UNIT_PREFIX else ""),
              file=sys.stderr)
        return 0

    station.resolvers[handle] = Resolver(
        handle=handle, instance=instance, profile=profile, checkout=checkout,
        env_file=env_path.name, bot_user_id=granted["user_id"], unit_prefix=prefix,
    )
    save_station(station)
    print(f"managed as {handle} on station '{station.name}'")

    if args.start:
        resolver = station.resolvers[handle]
        if not units.templates_installed(prefix):
            units.install_templates(resolver_dir, prefix)
        units.start(resolver)
        print(f"started {resolver.timer_unit} + {resolver.listener_unit}")
    else:
        print(f"start it with: stingray station start {handle}")
    return 0


def cmd_forget(args) -> int:
    station = load_station()
    resolver = station.get(args.name)  # raises with the known names if absent
    del station.resolvers[resolver.handle]
    save_station(station)
    print(f"forgot {resolver.handle} — nothing on disk or in systemd was changed")
    return 0


def cmd_ls(args) -> int:
    station = load_station()
    if not station.resolvers:
        print(f"station '{station.name}' manages nothing yet — "
              f"run: stingray station init")
        return 0
    rows = _status_rows(station, sorted(station.resolvers))
    print(f"station '{station.name}' — {stations_path()}")
    print(f"{'NAME':<20} {'BOT':<5} {'PROFILE':<12} {'STATE':<20} CHECKOUT")
    for st in rows:
        bot = str(st.bot_user_id or st.resolver.bot_user_id or "?")
        print(f"{st.resolver.handle:<20} {bot:<5} {st.resolver.profile:<12} "
              f"{st.summary:<20} {st.git}")
    return 0


def _print_status(st: ResolverStatus) -> None:
    r = st.resolver
    bot = st.bot_user_id or r.bot_user_id or "?"
    print(f"{r.handle}   bot {bot}   {r.profile}  ({st.stingray_url or 'url unset'})")
    for label, state, extra in (
        ("timer", st.timer, f"next {st.next_sweep}" if st.next_sweep else ""),
        ("listener", st.listener,
         (f"since {_fmt_since(st.listener)}" if st.listener.ok else "")
         + (f", {st.listener.restarts} restarts" if st.listener.restarts else "")),
    ):
        if not state.loaded:
            print(f"  {label:<10} no unit template ({state.name})")
            continue
        if not state.installed:
            print(f"  {label:<10} not enabled")
            continue
        print(f"  {label:<10} {state.active:<9} {extra}".rstrip())
    if st.last_sweep_line:
        print(f"  {'sweep':<10} {st.last_sweep_line}")
    if st.listener_line:
        print(f"  {'stream':<10} {st.listener_line}")
    print(f"  {'checkout':<10} {st.git}  ({r.checkout})")
    print(f"  {'identity':<10} {r.env_file}" + ("" if st.env_present else "  ⚠ missing"))


def cmd_status(args) -> int:
    station = load_station()
    names = [args.name] if args.name else sorted(station.resolvers)
    if not names:
        print("nothing managed yet — run: stingray station init")
        return 0
    for i, st in enumerate(_status_rows(station, names)):
        if i:
            print()
        _print_status(st)
    return 0


def cmd_start(args) -> int:
    station = load_station()
    resolver = station.get(args.name)
    if not units.templates_installed(resolver.unit_prefix):
        written = units.install_templates(resolver.resolver_dir, resolver.unit_prefix)
        print(f"installed {len(written)} unit templates for {resolver.unit_prefix}@")
    units.start(resolver)
    print(f"started {resolver.handle}: {resolver.timer_unit} + "
          f"{resolver.listener_unit}")
    return 0


def cmd_stop(args) -> int:
    station = load_station()
    resolver = station.get(args.name)
    units.stop(resolver)
    print(f"stopped {resolver.handle} — the timer is disabled too, so nothing will "
          f"pick its work up until it is started again")
    return 0


def cmd_restart(args) -> int:
    station = load_station()
    resolver = station.get(args.name)
    units.restart(resolver)
    print(f"restarted {resolver.listener_unit}")
    return 0


def cmd_sweep(args) -> int:
    station = load_station()
    resolver = station.get(args.name)
    result = units.sweep_now(resolver)
    if result.returncode != 0:
        print(f"error: {(result.stderr or '').strip()}", file=sys.stderr)
        return 1
    print(f"ran {resolver.sweep_unit}; see: stingray station logs {resolver.handle}")
    return 0


def cmd_logs(args) -> int:
    station = load_station()
    resolver = station.get(args.name)
    path = resolver.sweep_log if args.which == "sweep" else resolver.listener_log
    if not path.exists():
        raise ConfigError(
            f"{path} does not exist yet — that unit has not produced output. "
            f"Check it is installed: stingray station status {resolver.handle}"
        )
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
        for line in lines[-args.lines:]:
            sys.stdout.write(line)
        if not args.follow:
            return 0
        sys.stdout.flush()
        try:
            while True:
                line = fh.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.4)
        except KeyboardInterrupt:
            print()
            return 130
