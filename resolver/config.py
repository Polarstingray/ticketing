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


def _bot_user_id() -> int:
    """The resolver's bot user id. Prefer the agent-neutral RESOLVER_BOT_USER_ID;
    fall back to the original CLAUDE_BOT_USER_ID for backward compatibility."""
    raw = (os.environ.get("RESOLVER_BOT_USER_ID")
           or os.environ.get("CLAUDE_BOT_USER_ID") or "").strip()
    if not raw:
        raise SystemExit(
            "resolver: missing required env var RESOLVER_BOT_USER_ID "
            "(or legacy CLAUDE_BOT_USER_ID); see .env.example"
        )
    return int(raw)


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
    agent: str
    projects_root: Path
    repo_map: dict[str, str]
    default_repo: str
    claude_bin: str
    claude_model: str
    implement_tools: str
    claude_timeout: int
    claude_implement_timeout: int
    patch_fallback: bool
    # --- follow-up ticketing (/ticket directive) ---
    default_reviewer_id: int | None
    followup_tags: list[str]
    # --- reliability / hygiene tunables ---
    stingray_max_retries: int
    max_attempts: int
    max_tickets_per_sweep: int
    git_net_timeout: int
    log_retention_days: int
    git_author_name: str
    git_author_email: str
    audit_output_tail_bytes: int
    logs_dir: Path = field(default_factory=lambda: HERE / "logs")

    @classmethod
    def load(cls) -> "Config":
        _load_env_file(HERE / ".env")
        cfg = cls(
            stingray_url=_require("STINGRAY_URL").rstrip("/"),
            api_key=_require("STINGRAY_API_KEY"),
            bot_user_id=_bot_user_id(),
            # Which agent runner drives plan/implement. "claude" today; a resolver
            # on another identity can set RESOLVER_AGENT to a registered runner.
            agent=os.environ.get("RESOLVER_AGENT", "claude").strip() or "claude",
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
            # The implement phase does strictly more than plan (edit + verify),
            # so give it a larger default budget than the (read-only) plan phase.
            claude_implement_timeout=int(
                os.environ.get("CLAUDE_IMPLEMENT_TIMEOUT", "2400")
            ),
            patch_fallback=os.environ.get("PATCH_FALLBACK", "0").strip() in ("1", "true", "yes"),
            # Who a bare `assign` on a `/ticket` directive resolves to when no
            # explicit id is given (falls back to the source ticket's creator if
            # unset). Blank = unset.
            default_reviewer_id=(
                int(os.environ["RESOLVER_DEFAULT_REVIEWER_ID"])
                if os.environ.get("RESOLVER_DEFAULT_REVIEWER_ID", "").strip()
                else None
            ),
            # Tags merged onto every resolver-filed follow-up ticket so they're
            # identifiable. Comma-separated; blank = no extra tags.
            followup_tags=[
                t.strip()
                for t in os.environ.get("RESOLVER_FOLLOWUP_TAGS", "resolver").split(",")
                if t.strip()
            ],
            # Retry transient Stingray API failures (connection/5xx/429) this many
            # times before giving up, so a network blip mid-sweep doesn't strand a
            # ticket in a claude:* in-flight state.
            stingray_max_retries=int(os.environ.get("STINGRAY_MAX_RETRIES", "3")),
            # Stop auto-retrying a ticket after this many failed plan/implement
            # attempts and hand it to a human, so a broken ticket can't burn tokens
            # on every cron tick forever. 0 disables the cap.
            max_attempts=int(os.environ.get("MAX_ATTEMPTS", "3")),
            # Cap how many tickets one sweep processes (0 = unlimited) so a backlog
            # doesn't serialize for hours under the flock; the next tick continues.
            max_tickets_per_sweep=int(os.environ.get("MAX_TICKETS_PER_SWEEP", "0")),
            # Longer timeout for network git/gh commands (push/fetch/pr create) so a
            # slow transfer isn't SIGKILLed mid-flight by the default run() budget.
            git_net_timeout=int(os.environ.get("GIT_NET_TIMEOUT", "300")),
            # Prune sweep/audit/ticket logs older than this many days at sweep start.
            log_retention_days=int(os.environ.get("LOG_RETENTION_DAYS", "14")),
            # Identity stamped on the resolver's commits, so the commit doesn't fail
            # on a host with no global git identity (which gets misreported as
            # "Claude produced no code changes").
            git_author_name=os.environ.get("GIT_AUTHOR_NAME", "Stingray Resolver").strip()
            or "Stingray Resolver",
            git_author_email=os.environ.get("GIT_AUTHOR_EMAIL", "resolver@stingray.local").strip()
            or "resolver@stingray.local",
            # Per-event cap on how much command/Claude output is copied into the
            # structured audit log (the full text still goes to per-ticket logs).
            audit_output_tail_bytes=int(os.environ.get("AUDIT_OUTPUT_TAIL_BYTES", "4096")),
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
