"""The station inventory: ``~/.config/stingray/stations.toml``.

    [station]
    name = "ubvm.home.lab"

    [resolver.gemini]
    profile     = "local"
    checkout    = "/home/penguin/projects/ticketing"
    env_file    = ".env.gemini"
    bot_user_id = 3
    unit_prefix = "stingray-resolver"

    ["resolver.claude-lite@home"]
    instance    = "claude-lite"     # the systemd instance; the key is the handle

Local-first on purpose. The station has to be usable when the server is
unreachable, which is exactly when someone needs it — so what runs here is
recorded here, and the server learns about it through the heartbeat rather than
the other way round.

Nothing derived is stored. Unit state, checkout ref, last sweep and server-side
settings are all read fresh on every command, because a cache of those is a
second thing that can be wrong.

``unit_prefix`` is recorded rather than inferred. Two checkouts on one host need
two prefixes (``stingray-resolver`` and ``stingray-ubvm``), and an identity name
can repeat across them — guessing is how a listener ends up poking the other
server's sweep unit.

The table key is a **handle**, station-unique, and is not necessarily the
systemd instance name. They differ only when they have to: this host really does
run a ``claude-lite`` against each of two servers, meaning two different bots, so
one of them is keyed ``claude-lite@home`` while both keep the instance name their
units and ``.env`` files already use. A command takes either, and says so when a
bare instance name is ambiguous.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from stingray_cli.config import ConfigError, write_secure

DEFAULT_UNIT_PREFIX = "stingray-resolver"

# Systemd instance names may not contain a "/" (it is the escape character for a
# path), and we additionally refuse whitespace and "@" so a name always round-
# trips through `stingray-resolver@<name>.service` unambiguously. "." is refused
# because a dotted key is a *nested table* in TOML, so `[resolver.a.b]` would
# read back as resolver "a" containing "b" rather than a resolver named "a.b".
_BAD_NAME_CHARS = set('/@. \t\n"\'')


def stations_path() -> Path:
    """Where the inventory lives. ``STINGRAY_STATIONS`` overrides (tests use it)."""
    override = os.environ.get("STINGRAY_STATIONS")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "stingray" / "stations.toml"


@dataclass
class Resolver:
    """One managed identity on this host.

    ``handle`` is how the operator names it here and is unique per station;
    ``instance`` is what systemd and the ``.env`` suffix call it, and is only
    unique within a unit family. They are usually the same string.
    """

    handle: str
    instance: str
    profile: str
    checkout: Path
    env_file: str
    bot_user_id: int | None = None
    unit_prefix: str = DEFAULT_UNIT_PREFIX

    @property
    def resolver_dir(self) -> Path:
        """The directory the units run from — always ``<checkout>/resolver``."""
        return self.checkout / "resolver"

    @property
    def env_path(self) -> Path:
        return self.resolver_dir / self.env_file

    @property
    def sweep_unit(self) -> str:
        return f"{self.unit_prefix}@{self.instance}.service"

    @property
    def timer_unit(self) -> str:
        return f"{self.unit_prefix}@{self.instance}.timer"

    @property
    def listener_unit(self) -> str:
        # The listener template is named `<prefix>-listen@`, so a renamed family
        # ("stingray-ubvm") carries its listener with it.
        return f"{self.unit_prefix}-listen@{self.instance}.service"

    @property
    def sweep_log(self) -> Path:
        return self.resolver_dir / "logs" / f"resolver-{self.instance}.log"

    @property
    def listener_log(self) -> Path:
        return self.resolver_dir / "logs" / f"listen-{self.instance}.log"


@dataclass
class Station:
    name: str
    resolvers: dict[str, Resolver]

    def get(self, name: str) -> Resolver:
        """Look up by handle, falling back to an unambiguous instance name.

        The fallback is what lets a name that happens to be unique just work,
        while a name that means two different bots on two different servers is
        refused rather than resolved by luck.
        """
        if name in self.resolvers:
            return self.resolvers[name]
        matches = [r for r in self.resolvers.values() if r.instance == name]
        if len(matches) == 1:
            return matches[0]
        if matches:
            handles = ", ".join(sorted(r.handle for r in matches))
            raise ConfigError(
                f"{name!r} is the instance name of more than one resolver here "
                f"({handles}). Use the handle."
            )
        known = ", ".join(sorted(self.resolvers)) or "none"
        raise ConfigError(
            f"no resolver named {name!r} on this station (known: {known}). "
            f"Adopt an existing one with: stingray station adopt {name} "
            f"--checkout DIR --env-file .env.{name}"
        )

    def by_bot(self, profile: str, bot_user_id: int) -> Resolver | None:
        """The identity already claiming this bot on this server, if any.

        Scoped by profile because bot ids repeat across servers — id 5 is a
        different account on each — so a bare id is never a unique key.
        """
        for r in self.resolvers.values():
            if r.profile == profile and r.bot_user_id == bot_user_id:
                return r
        return None


def validate_name(name: str, *, handle: bool = False) -> str:
    """Reject a name that cannot be a systemd instance or an ``.env`` suffix.

    A ``handle`` is only a key in this file, so it may carry the ``@`` that
    disambiguates one server's ``claude-lite`` from another's; an instance name
    may not, since ``@`` is what systemd splits a template on.
    """
    if not name:
        raise ConfigError("resolver name must not be empty")
    forbidden = _BAD_NAME_CHARS - {"@"} if handle else _BAD_NAME_CHARS
    bad = sorted(forbidden & set(name))
    if bad:
        raise ConfigError(
            f"resolver name {name!r} contains {''.join(bad)!r}; it becomes a systemd "
            "instance name, a TOML key and an .env suffix, so use letters, digits "
            "and -"
        )
    return name


def _read_raw() -> dict:
    path = stations_path()
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def load_station(*, required: bool = True) -> Station:
    """Read the inventory. With ``required``, an absent one is an error."""
    raw = _read_raw()
    if not raw:
        if required:
            raise ConfigError(
                f"no station configured on this host ({stations_path()} is missing). "
                "Create one with: stingray station init"
            )
        return Station(name="", resolvers={})

    resolvers: dict[str, Resolver] = {}
    for handle, stanza in (raw.get("resolver") or {}).items():
        missing = [k for k in ("profile", "checkout", "env_file") if not stanza.get(k)]
        if missing:
            raise ConfigError(
                f"[resolver.{handle}] in {stations_path()} is missing "
                f"{', '.join(missing)}"
            )
        resolvers[handle] = Resolver(
            handle=handle,
            instance=stanza.get("instance") or handle,
            profile=stanza["profile"],
            checkout=Path(stanza["checkout"]).expanduser(),
            env_file=stanza["env_file"],
            bot_user_id=stanza.get("bot_user_id"),
            unit_prefix=stanza.get("unit_prefix", DEFAULT_UNIT_PREFIX),
        )
    return Station(name=(raw.get("station") or {}).get("name", ""), resolvers=resolvers)


def _quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dump(station: Station) -> str:
    lines = ["[station]", f"name = {_quote(station.name)}", ""]
    for handle in sorted(station.resolvers):
        r = station.resolvers[handle]
        # Quoted key: validate_name already forbids a dot, and quoting makes that
        # belt-and-braces rather than the only thing standing between a name and
        # a silently nested table.
        lines.append(f"[resolver.{_quote(handle)}]")
        if r.instance != handle:
            lines.append(f"instance = {_quote(r.instance)}")
        lines.append(f"profile = {_quote(r.profile)}")
        lines.append(f"checkout = {_quote(str(r.checkout))}")
        lines.append(f"env_file = {_quote(r.env_file)}")
        if r.bot_user_id is not None:
            lines.append(f"bot_user_id = {r.bot_user_id}")
        lines.append(f"unit_prefix = {_quote(r.unit_prefix)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_station(station: Station) -> Path:
    """Write the inventory back, 0600 like the credential store beside it."""
    return write_secure(stations_path(), _dump(station))
