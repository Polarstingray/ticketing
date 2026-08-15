"""Credential store for the ``stingray`` CLI.

Profiles live in ``~/.config/stingray/config.toml`` at mode 0600:

    default_profile = "local"

    [profile.local]
    url = "http://localhost:3000"
    api_key = "sk_..."
    bot_user_id = 2

Precedence, highest first: explicit flags, then ``STINGRAY_URL`` /
``STINGRAY_API_KEY`` in the environment, then the selected profile.

Note this is the *opposite* of ``resolver/config.py``, where the ``.env`` file
deliberately wins over the ambient environment: the resolver is a daemon whose
identity must not be perturbed by whatever is exported in a shell, whereas an
interactive CLI should honor an explicit override. Don't "fix" one to match the
other.
"""
from __future__ import annotations

import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """Actionable configuration problem — printed without a traceback."""


def config_path() -> Path:
    """Where the config lives. ``STINGRAY_CONFIG`` overrides (used by tests)."""
    override = os.environ.get("STINGRAY_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "stingray" / "config.toml"


@dataclass
class Profile:
    name: str
    url: str
    api_key: str
    bot_user_id: int | None = None
    user_id: int | None = None
    username: str = ""
    scopes: list[str] = field(default_factory=list)
    describe: dict = field(default_factory=dict)

    @property
    def key_display(self) -> str:
        """The non-secret prefix, matching what the server stores for display."""
        return f"{self.api_key[:11]}…" if self.api_key else "(none)"


def _read_raw() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        # Group/other can read a live API key. Warn loudly rather than failing —
        # the user may be mid-repair and still needs `auth status` to work.
        print(
            f"warning: {path} is mode {mode:o}; it holds an API key. "
            f"Fix with: chmod 600 {path}",
            file=sys.stderr,
        )
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def load_profile(name: str | None = None, *, url: str | None = None,
                 api_key: str | None = None) -> Profile:
    """Resolve the active profile, applying flag > env > file precedence.

    ``name`` selects a profile stanza; without it, ``STINGRAY_PROFILE`` then
    ``default_profile`` decide. Flags and env vars can stand in for a stored
    profile entirely, so the CLI works in CI with no config file at all.
    """
    raw = _read_raw()
    profiles = raw.get("profile", {})
    selected = name or os.environ.get("STINGRAY_PROFILE") or raw.get("default_profile")

    stanza: dict = {}
    if selected:
        if selected not in profiles and not (url or api_key):
            known = ", ".join(sorted(profiles)) or "none"
            raise ConfigError(
                f"no profile named {selected!r} in {config_path()} (known: {known}). "
                f"Create one with: stingray auth login --profile {selected} --url URL"
            )
        stanza = profiles.get(selected, {})

    resolved_url = url or os.environ.get("STINGRAY_URL") or stanza.get("url", "")
    resolved_key = api_key or os.environ.get("STINGRAY_API_KEY") or stanza.get("api_key", "")

    if not resolved_url or not resolved_key:
        raise ConfigError(
            "not authenticated. Run: stingray auth login --url http://localhost:3000\n"
            "(or set STINGRAY_URL and STINGRAY_API_KEY)"
        )

    return Profile(
        name=selected or "(env)",
        url=resolved_url.rstrip("/"),
        api_key=resolved_key,
        bot_user_id=stanza.get("bot_user_id"),
        user_id=stanza.get("user_id"),
        username=stanza.get("username", ""),
        scopes=list(stanza.get("scopes", [])),
        describe=dict(stanza.get("describe", {})),
    )


def _quote(value: str) -> str:
    """TOML basic string. The values we store are URLs, keys and usernames."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _dump(raw: dict) -> str:
    """Serialize our small, flat config shape.

    Hand-rolled rather than pulling in a TOML *writer* dependency: the schema is
    a handful of scalars under `[profile.<name>]` plus one nested table.
    """
    lines: list[str] = []
    if raw.get("default_profile"):
        lines.append(f"default_profile = {_quote(raw['default_profile'])}")
        lines.append("")
    for name, stanza in sorted(raw.get("profile", {}).items()):
        lines.append(f"[profile.{name}]")
        for key, value in stanza.items():
            if key == "describe":
                continue
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            elif isinstance(value, int):
                lines.append(f"{key} = {value}")
            elif isinstance(value, list):
                items = ", ".join(_quote(str(v)) for v in value)
                lines.append(f"{key} = [{items}]")
            else:
                lines.append(f"{key} = {_quote(str(value))}")
        describe = stanza.get("describe") or {}
        if describe:
            lines.append("")
            lines.append(f"[profile.{name}.describe]")
            for key, value in describe.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    lines.append(f"{key} = {value}")
                else:
                    lines.append(f"{key} = {_quote(str(value))}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write(raw: dict) -> Path:
    """Write the config at 0600, creating the directory at 0700.

    Opened with the mode up front rather than written-then-chmod'd: the latter
    leaves a window where a live API key sits in a world-readable file.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_dump(raw))
    except Exception:
        os.close(fd)
        raise
    # An existing file keeps its old mode through O_CREAT, so pin it explicitly.
    os.chmod(path, 0o600)
    return path


def save_profile(name: str, values: dict, *, make_default: bool | None = None) -> Path:
    """Merge ``values`` into profile ``name`` and write the file back."""
    raw = _read_raw()
    profiles = raw.setdefault("profile", {})
    stanza = profiles.setdefault(name, {})
    stanza.update({k: v for k, v in values.items() if v is not None})
    if make_default or (make_default is None and not raw.get("default_profile")):
        raw["default_profile"] = name
    return _write(raw)


def delete_profile(name: str) -> bool:
    """Drop a profile. Returns False if it wasn't there."""
    raw = _read_raw()
    profiles = raw.get("profile", {})
    if name not in profiles:
        return False
    del profiles[name]
    if raw.get("default_profile") == name:
        raw["default_profile"] = next(iter(sorted(profiles)), None)
    _write(raw)
    return True


def list_profiles() -> tuple[dict, str | None]:
    raw = _read_raw()
    return raw.get("profile", {}), raw.get("default_profile")
