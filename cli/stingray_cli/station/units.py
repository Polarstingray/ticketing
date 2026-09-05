"""systemd is the supervisor; this module only drives it.

Deliberately not a process supervisor of its own. systemd already gives the
resolver the things a hand-rolled one would have to reimplement badly: it
serializes runs of the same unit, merges a start request into a job already
queued (which is what makes a burst of ticket assignments one sweep instead of
N), restarts a dead listener, survives logout via linger, and comes back at
boot. A station that spawned its own children would lose all of that and die
with the terminal.

Everything here runs ``systemctl --user``. User units are the right home for a
resolver — the agent CLIs need the account's own credentials — and a non-root
process cannot start a *system* unit without a polkit rule, so the ``--user``
scope is load-bearing rather than a preference.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from stingray_cli.config import ConfigError
from stingray_cli.station.inventory import Resolver

# The three templates a checkout ships, and the placeholder path baked into
# them. `resolver/stingray-resolver@.service` documents the same sed by hand.
TEMPLATES = (
    "stingray-resolver@.service",
    "stingray-resolver@.timer",
    "stingray-resolver-listen@.service",
)
TEMPLATE_ROOT = "/opt/ticketing/resolver"
TEMPLATE_PREFIX = "stingray-resolver"

_TIMEOUT = 30


def user_unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "systemd" / "user"


def systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run one ``systemctl --user`` command.

    Never through a shell: unit names reach us from a config file, and the whole
    point of systemd templating is that ``%i`` is a literal rather than
    something an operator can smuggle a command through.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except FileNotFoundError:
        raise ConfigError(
            "systemctl not found — a station manages systemd user units, so this "
            "host needs systemd."
        ) from None
    except subprocess.TimeoutExpired:
        raise ConfigError(f"`systemctl --user {' '.join(args)}` timed out") from None
    if check and result.returncode != 0:
        raise ConfigError(
            f"`systemctl --user {' '.join(args)}` failed "
            f"({result.returncode}): {(result.stderr or '').strip()}"
        )
    return result


def show(unit: str, *properties: str) -> dict[str, str]:
    """``systemctl show`` for one unit, as a dict.

    A unit that does not exist is not an error here — it shows up as empty or
    ``LoadState=not-found``, which is exactly the state ``status`` wants to
    report rather than raise on.
    """
    args = ["show", unit]
    for prop in properties:
        args += ["-p", prop]
    result = systemctl(*args)
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            out[key] = value
    return out


@dataclass
class UnitState:
    name: str
    loaded: bool
    active: str = "unknown"      # active / inactive / failed / unknown
    sub: str = ""                # running / dead / waiting / ...
    enabled: str = ""            # enabled / disabled / static / ...
    since: str = ""              # ActiveEnterTimestamp
    restarts: int = 0
    next_elapse: str = ""        # timers only
    result: str = ""             # oneshot services: success / exit-code / ...

    @property
    def ok(self) -> bool:
        return self.loaded and self.active == "active"

    @property
    def installed(self) -> bool:
        """Whether this *instance* has been enabled, not merely instantiable.

        `LoadState` is a trap for templated units: systemd reports `loaded` for
        every conceivable instance once the template file exists, so
        `stingray-ubvm@nonexistent.timer` looks as real as a running one. What
        distinguishes an instance the operator actually asked for is an enable
        symlink, or being active right now.
        """
        return self.enabled.startswith("enabled") or self.active == "active"


def unit_state(unit: str) -> UnitState:
    props = show(
        unit,
        "LoadState", "ActiveState", "SubState", "UnitFileState",
        "ActiveEnterTimestamp", "NRestarts", "NextElapseUSecRealtime", "Result",
    )
    loaded = props.get("LoadState", "not-found") == "loaded"
    return UnitState(
        name=unit,
        loaded=loaded,
        active=props.get("ActiveState", "unknown"),
        sub=props.get("SubState", ""),
        enabled=props.get("UnitFileState", ""),
        since=props.get("ActiveEnterTimestamp", ""),
        restarts=int(props.get("NRestarts") or 0),
        next_elapse=props.get("NextElapseUSecRealtime", ""),
        result=props.get("Result", ""),
    )


def timer_next(resolver: Resolver) -> str:
    """When the sweep timer fires next, as systemd prints it (or "")."""
    result = systemctl("list-timers", resolver.timer_unit, "--all", "--no-pager")
    for line in result.stdout.splitlines():
        if resolver.timer_unit in line:
            # "NEXT" is the first three whitespace-separated fields of the row.
            parts = line.split()
            if parts and parts[0] != "-":
                return " ".join(parts[:3])
    return ""


# --- template installation --------------------------------------------------

def render(template_text: str, resolver_dir: Path, prefix: str) -> str:
    """Substitute a template for one checkout and unit family.

    Order matters only in that the path substitution must not disturb the unit
    names; it does not, since the placeholder path contains no unit name. Both
    replacements are global, which is what makes the listener's ``--unit`` and
    ``Wants=`` follow the family rename instead of pointing back at the original.
    """
    out = template_text.replace(TEMPLATE_ROOT, str(resolver_dir))
    if prefix != TEMPLATE_PREFIX:
        out = out.replace(TEMPLATE_PREFIX, prefix)
    return out


def installed_names(prefix: str) -> list[str]:
    return [t.replace(TEMPLATE_PREFIX, prefix) for t in TEMPLATES]


def install_templates(resolver_dir: Path, prefix: str) -> list[Path]:
    """Render the checkout's three templates into the user unit directory."""
    dest_dir = user_unit_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for template in TEMPLATES:
        src = resolver_dir / template
        if not src.is_file():
            raise ConfigError(
                f"{src} is missing — this checkout predates the templated units. "
                "Pull the branch that adds them, or adopt an identity you have "
                "already installed by hand."
            )
        dest = dest_dir / template.replace(TEMPLATE_PREFIX, prefix)
        dest.write_text(render(src.read_text(encoding="utf-8"), resolver_dir, prefix),
                        encoding="utf-8")
        written.append(dest)
    systemctl("daemon-reload", check=True)
    return written


def templates_installed(prefix: str) -> bool:
    dest_dir = user_unit_dir()
    return all((dest_dir / name).is_file() for name in installed_names(prefix))


# --- lifecycle --------------------------------------------------------------

def start(resolver: Resolver) -> None:
    """Bring the identity up: the timer's cadence and the listener's stream.

    Both, because "a resolver" is one thing to an operator. The pair stays
    visible in ``status``, so a half-up state is reported rather than hidden.
    """
    systemctl("enable", "--now", resolver.timer_unit, check=True)
    systemctl("enable", "--now", resolver.listener_unit, check=True)


def stop(resolver: Resolver) -> None:
    systemctl("disable", "--now", resolver.listener_unit, check=True)
    systemctl("disable", "--now", resolver.timer_unit, check=True)


def restart(resolver: Resolver) -> None:
    systemctl("restart", resolver.listener_unit, check=True)


def sweep_now(resolver: Resolver) -> subprocess.CompletedProcess:
    """Run one sweep immediately, the way the listener's poke does.

    ``--no-block`` is not optional. The sweep unit is ``Type=oneshot``, so a
    plain ``start`` waits for the whole sweep — which can be an agent run of
    several minutes — and this call would time out while the work it asked for
    was going perfectly well. It is also what lets systemd merge this request
    into a start already queued, which is the debouncing the listener relies on.
    """
    return systemctl("start", "--no-block", resolver.sweep_unit)
