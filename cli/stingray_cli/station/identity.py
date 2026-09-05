"""Reading the ``.env.<name>`` files that give each resolver its identity.

Lifted from ``resolver/cli.py`` so the station can read an identity without
importing anything out of a resolver checkout — there are two checkouts on this
host running two different revisions, and a manager that imported one of them
would describe the other through the wrong lens.

Read-mostly by design. The values here include the resolver's API key, so this
module never prints a whole file and never copies one to the server.
"""
from __future__ import annotations

from pathlib import Path

# Keys that must never be shown in full or sent anywhere. The server refuses
# them too (`ResolverSettingsUpdate` sets extra="forbid"), but the station is
# the thing holding the plaintext, so the guard belongs here first.
SECRET_KEYS = frozenset({
    "STINGRAY_API_KEY", "REVIEW_API_KEY", "CRITIQUE_API_KEY",
    "DIGEST_ADMIN_KEY", "AGENT_PROVIDER_KEYS",
})

DESC_KEY = "RESOLVER_BOT_DESC"


def read_env(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file into a dict. No environ mutation, no interpolation."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def redact(key: str, value: str) -> str:
    """A secret rendered as its non-secret prefix, the way the server shows keys."""
    if key not in SECRET_KEYS or not value:
        return value
    return f"{value[:11]}…" if len(value) > 12 else "(set)"


def identity_name(env_file: str) -> str:
    """``.env`` -> 'default'; ``.env.gemini`` -> 'gemini'.

    Mirrors ``resolver/config.py:_identity_name`` so the roster, the resolver
    and this tool agree on a short name.
    """
    base = Path(env_file).name
    if base == ".env":
        return "default"
    return base[len(".env."):] if base.startswith(".env.") else base


def discover(resolver_dir: Path) -> list[Path]:
    """Every identity file in a resolver directory, newest naming first.

    ``.env.example`` is the template, not an identity. A bare ``.env`` *is* one,
    but it cannot be a systemd instance name — the caller is expected to offer a
    ``.env.<name>`` symlink for it rather than silently skipping it.
    """
    if not resolver_dir.is_dir():
        return []
    return sorted(
        p for p in resolver_dir.glob(".env*")
        if p.is_file() and is_identity_file(p.name)
    )


def is_identity_file(filename: str) -> bool:
    """``.env`` or ``.env.<one segment>`` — nothing else.

    The single-segment rule is what separates an identity from the backups that
    accumulate beside it (`.env.mistral-bot.bak-critique-fix`, `.env.x.save`).
    It is not cosmetic: a second dot would also become a nested table in the
    inventory's TOML and a confusing systemd instance name, so a file with one
    is never something this tool should manage.
    """
    if filename == ".env.example" or not filename.startswith(".env"):
        return False
    if filename == ".env":
        return True
    if not filename.startswith(".env."):
        return False
    return "." not in filename[len(".env."):]
