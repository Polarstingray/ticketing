"""Configuration loading and repo-allowlist enforcement for the resolver.

Keeps to the stdlib (plus `requests` elsewhere). The `.env` file next to this
module is parsed manually so we don't need python-dotenv.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


HERE = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Populate os.environ from a simple KEY=VALUE .env file (no export, no
    interpolation). Keys present in the file win over the ambient environment —
    this .env is the resolver's source of truth, which also avoids picking up a
    stale ambient STINGRAY_URL. Keys absent from the file fall through to the
    real environment, so deployments can still inject secrets that way."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(f"resolver: missing required env var {name} (see .env.example)")
    return val


def _parse_repo_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, path = chunk.partition("=")
        mapping[name.strip()] = path.strip()
    return mapping


@dataclass
class Config:
    stingray_url: str
    api_key: str
    bot_user_id: int
    projects_root: Path
    repo_map: dict[str, str]
    default_repo: str
    claude_bin: str
    claude_model: str
    implement_tools: str
    claude_timeout: int
    patch_fallback: bool
    logs_dir: Path = field(default_factory=lambda: HERE / "logs")

    @classmethod
    def load(cls) -> "Config":
        _load_env_file(HERE / ".env")
        cfg = cls(
            stingray_url=_require("STINGRAY_URL").rstrip("/"),
            api_key=_require("STINGRAY_API_KEY"),
            bot_user_id=int(_require("CLAUDE_BOT_USER_ID")),
            projects_root=Path(_require("PROJECTS_ROOT")).resolve(),
            repo_map=_parse_repo_map(os.environ.get("REPO_MAP", "")),
            default_repo=os.environ.get("DEFAULT_REPO", "").strip(),
            claude_bin=os.environ.get("CLAUDE_BIN", "claude").strip() or "claude",
            claude_model=os.environ.get("CLAUDE_MODEL", "").strip(),
            implement_tools=os.environ.get(
                "CLAUDE_IMPLEMENT_TOOLS",
                # Broad Bash so Claude can run tests/build in the worktree;
                # isolation comes from the worktree + PROJECTS_ROOT allowlist,
                # not from narrowing Bash (compound `cd && cmd` defeats that).
                "Edit Write Read Glob Grep Bash",
            ).strip(),
            claude_timeout=int(os.environ.get("CLAUDE_TIMEOUT", "1800")),
            patch_fallback=os.environ.get("PATCH_FALLBACK", "0").strip() in ("1", "true", "yes"),
        )
        cfg.logs_dir.mkdir(exist_ok=True)
        if not cfg.projects_root.is_dir():
            raise SystemExit(f"resolver: PROJECTS_ROOT does not exist: {cfg.projects_root}")
        return cfg

    def resolve_repo(self, repo_name: str | None) -> Path:
        """Map a `repo:<name>` value (or the default) to an absolute path and
        enforce the PROJECTS_ROOT allowlist. Raises RepoNotAllowed on escape and
        RepoNotFound when the directory is missing."""
        name = (repo_name or self.default_repo or "").strip()
        if not name:
            raise RepoNotFound("no repo specified (add a `repo:<name>` tag) and DEFAULT_REPO is unset")

        if name in self.repo_map:
            candidate = Path(self.repo_map[name])
        else:
            # A bare name is always taken relative to the allowlist root; a name
            # containing path separators is rejected outright.
            if "/" in name or name in ("..", "."):
                raise RepoNotAllowed(f"repo name {name!r} must be a plain directory name under PROJECTS_ROOT")
            candidate = self.projects_root / name

        resolved = candidate.expanduser().resolve()
        # Allowlist: the resolved path must be inside PROJECTS_ROOT. Using the
        # resolved (symlink-followed) path defeats traversal and symlink escapes.
        if resolved != self.projects_root and self.projects_root not in resolved.parents:
            raise RepoNotAllowed(
                f"repo {name!r} -> {resolved} is outside the allowlist {self.projects_root}"
            )
        if not resolved.is_dir():
            raise RepoNotFound(f"repo {name!r} -> {resolved} does not exist")
        if not (resolved / ".git").exists():
            raise RepoNotFound(f"repo {name!r} -> {resolved} is not a git repository")
        return resolved


class RepoNotAllowed(Exception):
    """Target repo resolved outside the PROJECTS_ROOT allowlist."""


class RepoNotFound(Exception):
    """Target repo could not be located (missing dir or not a git repo)."""
