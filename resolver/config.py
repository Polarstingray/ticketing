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


def _env(*names: str, default: str = "") -> str:
    """First non-empty env var among `names`, else `default`. Lets agent-neutral
    AGENT_* names take precedence while still honoring the legacy CLAUDE_* names
    (mirrors the RESOLVER_BOT_USER_ID <- CLAUDE_BOT_USER_ID fallback)."""
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return default


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


def _cron_log_path() -> "Path | None":
    """This resolver's cron stdout log, for size-rotation. A relative path is
    taken next to this module (matching logs_dir). Unset = no rotation."""
    raw = os.environ.get("CRON_LOG", "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else HERE / p


def rotate_cron_log_early() -> bool:
    """Size-rotate this resolver's cron stdout log *before* anything that can fail
    has been imported. Returns True if it rotated.

    ``audit.maintain_logs`` already does this every sweep, but it runs once the whole
    module has imported and a config has loaded. That is too late for the failure it
    most needs to survive: an import-time crash (a stale venv, a dependency that moved)
    makes cron relaunch every few minutes, each run appending a traceback to a log that
    can now never rotate itself. The one log guaranteed to grow without bound is the one
    belonging to a resolver that cannot start.

    So this deliberately duplicates a little of Config.load: it reads the env file
    directly and touches nothing but stdlib, which is what lets it run first. It is
    also safe to run twice — the later sweep-time rotation simply finds the file under
    the cap and does nothing."""
    env_file = os.environ.get("RESOLVER_ENV_FILE", "").strip() or ".env"
    env_path = Path(env_file)
    if not env_path.is_absolute():
        env_path = HERE / env_path
    _load_env_file(env_path)

    path = _cron_log_path()
    if path is None:
        return False
    try:
        max_bytes = int(os.environ.get("CRON_LOG_MAX_BYTES", "5000000"))
    except ValueError:
        max_bytes = 5_000_000
    if max_bytes <= 0:
        return False
    try:
        if path.stat().st_size <= max_bytes:
            return False
        # Rename rather than truncate: cron's `>>` keeps writing into the renamed
        # inode, and the next tick opens a fresh file. Same rationale as
        # audit.rotate_cron_log, which this mirrors.
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        return False
    return True


def _identity_name(env_file: str) -> str:
    """`.env` -> 'default'; `.env.gemini` -> 'gemini'. Mirrors cli._identity_name
    so the manager roster and the CLI agree on a resolver's short name."""
    base = Path(env_file).name
    return "default" if base == ".env" else base[len(".env."):] if base.startswith(".env.") else base


def _split_models(*raws: str) -> list[str]:
    """Flatten one or more comma-separated model lists into an ordered, de-duped
    list, dropping blanks. Order is preserved (first occurrence wins) so the
    primary->fallback escalation is deterministic."""
    out: list[str] = []
    for raw in raws:
        for name in (raw or "").split(","):
            name = name.strip()
            if name and name not in out:
                out.append(name)
    return out


def _parse_repo_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, path = chunk.partition("=")
        mapping[name.strip()] = path.strip()
    return mapping


def _parse_workers(raw: str) -> list[dict]:
    """Parse the delegation roster RESOLVER_WORKERS into an ordered list of
    `{id, name, desc}`. Entries are semicolon-separated; each is `id:name:desc`
    where desc (a short capability blurb) may contain spaces. A lead resolver
    renders this so its agent can pick which resolver to hand each sub-task to.

      RESOLVER_WORKERS=2:claude:heavy refactors & multi-file changes;\
                       3:open:cheap mechanical single-file fixes
    """
    workers: list[dict] = []
    for chunk in (raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":", 2)
        if len(parts) < 2:
            continue  # need at least id:name
        id_s, name = parts[0].strip(), parts[1].strip()
        desc = parts[2].strip() if len(parts) == 3 else ""
        try:
            uid = int(id_s)
        except ValueError:
            continue  # skip malformed ids rather than crash a sweep
        if name:
            workers.append({"id": uid, "name": name, "desc": desc})
    return workers


@dataclass
class Config:
    stingray_url: str
    api_key: str
    bot_user_id: int
    # This instance's identity, for the resolver-manager registry heartbeat.
    # env_file is the RESOLVER_ENV_FILE it was launched with (".env", ".env.gemini");
    # name is a clean label (".env"->"default", ".env.gemini"->"gemini"), overridable
    # with RESOLVER_NAME. Neither affects behavior — they're for the manager UI.
    env_file: str
    name: str
    agent: str
    projects_root: Path
    repo_map: dict[str, str]
    default_repo: str
    # --- agent invocation (agent-neutral; legacy CLAUDE_* names still honored) ---
    agent_bin: str
    agent_model: str
    # Optional per-phase model overrides; each falls back to agent_model when blank.
    # Lets the read-only plan/review phases run on a cheaper model than implement.
    agent_plan_model: str
    agent_implement_model: str
    agent_review_model: str
    # Difficulty-routed implement tiers: when the plan self-assesses an `easy`/`hard`
    # ticket, the implement phase swaps to these instead of agent_implement_model.
    # Blank = no swap (fall back to agent_implement_model -> agent_model), so routing
    # is opt-in. `hard` only swaps when escalation is disabled; otherwise hard tickets
    # escalate to escalate_to_user_id. See parse_difficulty / do_implement.
    agent_implement_model_easy: str
    agent_implement_model_hard: str
    agent_fallback_model: str
    # Ordered list of models to try after the primary before giving up (and handing
    # the ticket back / to another resolver). Parsed from AGENT_FALLBACK_MODELS
    # (comma-separated); the legacy singular AGENT_FALLBACK_MODEL is appended for
    # back-compat. Lets an unreliable free model fall through several alternatives
    # instead of one. See run_opencode.
    agent_fallback_models: list[str]
    implement_tools: str
    agent_timeout: int
    agent_implement_timeout: int
    # The read-only plan/review phases are exploration only and should finish in a
    # couple of minutes; a much shorter cap than implement means a hung/stalled model
    # fails over to the next in the fallback chain quickly instead of holding the
    # resolver lock for the full agent_timeout. See _phase_timeout.
    agent_plan_review_timeout: int
    # opencode-specific: which named agents drive the read-only plan phase and the
    # edit-capable implement phase (built-in "plan"/"build"; unused by Claude).
    opencode_plan_agent: str
    opencode_build_agent: str
    patch_fallback: bool
    # --- reliability / hygiene tunables ---
    stingray_max_retries: int
    max_attempts: int
    max_tickets_per_sweep: int
    git_net_timeout: int
    log_retention_days: int
    # Loose per-sweep/ticket logs from days older than this get rolled into a
    # daily archive/<date>.tar.gz; the archives are deleted after retention.
    log_archive_after_days: int
    # Optional path to this resolver's cron stdout log (its own per bot). When
    # set, it is size-rotated to <path>.1 at sweep start. None = no rotation.
    cron_log: Path | None
    cron_log_max_bytes: int
    git_author_name: str
    git_author_email: str
    audit_output_tail_bytes: int
    # --- free-resolver: difficulty routing -------------------------------------
    # When set, this (free) resolver hands tickets it deems out of scope to that
    # user (the Claude bot) instead of working them itself — see _should_escalate.
    # 0 = disabled (the Claude resolver leaves it unset, so there's no ping-pong).
    escalate_to_user_id: int
    # Priorities that escalate to escalate_to_user_id (in addition to the always-on
    # `dangerous` / `claude` tag triggers). Comma-separated; default high,critical.
    escalate_priorities: list[str]
    # --- /consolidate directive --------------------------------------------------
    # Who the code-review ticket filed after a `/consolidate` run is assigned to.
    # Defaults to 4 (claude-lite-ubvm), per the ticket's explicit ask; overridable
    # per-deployment/per-bot via CONSOLIDATE_REVIEW_USER_ID.
    consolidate_review_user_id: int
    # --- free-resolver: single-shot review backend -----------------------------
    # When all three are set, code reviews go through a direct OpenAI-compatible
    # chat completion (no opencode agent loop) — see single_shot_review. Works with
    # Groq / Mistral / OpenRouter etc. Empty = use the configured agent for reviews.
    review_api_url: str
    review_api_key: str
    review_api_model: str
    # --- free-resolver: plan-critique gate -------------------------------------
    # When all three are set, a cheap chat-completion model vets each freshly
    # produced plan before the human sees it (see run_critique). On a REVISE verdict
    # the planner is re-invoked with the critique notes, up to critique_max_revisions
    # times. Empty disables the gate. Same OpenAI-compatible shape as review_api_*.
    critique_api_url: str
    critique_api_key: str
    critique_api_model: str
    critique_max_revisions: int
    # Verification gate: a shell command the resolver runs in the worktree after an
    # implement run to confirm the agent's changes actually pass. Empty disables the
    # gate (implement publishes as soon as there's a diff, the legacy behavior).
    verify_command: str
    verify_timeout: int
    verify_max_retries: int
    # Quota backoff: when an agent run fails on an API quota/rate limit (rather than
    # a real error), the resolver parks the ticket — keeping it assigned to the bot
    # and preserving its phase tag — instead of handing it back to the user. The
    # sweep skips the ticket until this many minutes have elapsed, then auto-retries
    # the same phase. A user can force an early retry by re-assigning the ticket.
    quota_backoff_minutes: int
    # --- resolver-to-resolver delegation (fan-out) -----------------------------
    # When True, a ticket tagged `delegate` lets the lead resolver decompose it into
    # sub-tasks and hand each to another resolver (no per-task human approval; the
    # human reviews the resulting PRs). Default off — delegation is strictly opt-in.
    allow_delegation: bool
    # Roster of resolvers a lead may delegate to: a list of {id, name, desc}. The
    # lead's orchestration prompt renders this so the agent routes each sub-task to
    # the right resolver (e.g. heavy→claude bot, cheap mechanical→open bot). Parsed
    # from RESOLVER_WORKERS. Empty ⇒ delegation has no targets and stays disabled.
    workers: list[dict]
    # Hard cap on sub-tasks one delegation run may file (enforced in file_ticket.py),
    # so a single orchestration can't spawn unbounded tickets / agent cost.
    max_delegations: int
    # --- daily digest (see digest.py) ------------------------------------------
    # The digest surveys the WHOLE backlog, so it needs an admin-user key: the
    # resolver's own key is non-admin and `_visible_tickets` would silently narrow
    # the survey to the bot's own queue. Empty ⇒ `digest.py` refuses to run.
    digest_admin_key: str
    # Prose backend for the digest — the same OpenAI-compatible shape as
    # review_api_*. Each falls back to its REVIEW_API_* counterpart when unset; with
    # neither configured the digest still files, just without the summary paragraph.
    digest_api_url: str
    digest_api_key: str
    digest_api_model: str
    logs_dir: Path = field(default_factory=lambda: HERE / "logs")

    @classmethod
    def load(cls, *, api_only: bool = False) -> "Config":
        """Build the config from the selected .env file.

        ``api_only=True`` is for tools that only talk to the Stingray API and never
        check a repo out or run an agent — ``digest.py`` today. Those need a URL and
        their own key, nothing else, so the three requirements that exist for the
        sweep (``STINGRAY_API_KEY``, ``RESOLVER_BOT_USER_ID``, ``PROJECTS_ROOT``, the
        last of which must name a real directory) are relaxed to defaults. Without
        this a host whose whole job is filing a digest against a remote instance
        would have to invent a bot id and an empty directory to satisfy checks
        nothing on its path ever reads.
        """
        # Which env file to load (default `.env`). Lets several resolver identities
        # share this one code dir — e.g. RESOLVER_ENV_FILE=.env.gemini selects the
        # opencode/Gemini bot's config. The selector is read from the real
        # environment; a relative path is taken next to this module.
        env_file = os.environ.get("RESOLVER_ENV_FILE", "").strip() or ".env"
        env_path = Path(env_file)
        if not env_path.is_absolute():
            env_path = HERE / env_path
        _load_env_file(env_path)
        cfg = cls(
            stingray_url=_require("STINGRAY_URL").rstrip("/"),
            api_key=_env("STINGRAY_API_KEY") if api_only else _require("STINGRAY_API_KEY"),
            bot_user_id=0 if api_only else _bot_user_id(),
            env_file=env_file,
            name=os.environ.get("RESOLVER_NAME", "").strip() or _identity_name(env_file),
            # Which agent runner drives plan/implement. "claude" today; a resolver
            # on another identity can set RESOLVER_AGENT to a registered runner.
            agent=os.environ.get("RESOLVER_AGENT", "claude").strip() or "claude",
            projects_root=Path(_env("PROJECTS_ROOT") or HERE).resolve() if api_only
            else Path(_require("PROJECTS_ROOT")).resolve(),
            repo_map=_parse_repo_map(os.environ.get("REPO_MAP", "")),
            default_repo=os.environ.get("DEFAULT_REPO", "").strip(),
            # Agent CLI binary/model. AGENT_* is the agent-neutral name; the legacy
            # CLAUDE_* names still work so existing Claude resolvers need no change.
            agent_bin=_env("AGENT_BIN", "CLAUDE_BIN", default="claude"),
            agent_model=_env("AGENT_MODEL", "CLAUDE_MODEL"),
            # Per-phase model overrides. Each is empty by default and falls back to
            # AGENT_MODEL (in model_for), so behavior is unchanged until one is set.
            # Use them to run the read-only plan/review phases on a cheaper model and
            # reserve the strongest model for implement.
            agent_plan_model=_env("AGENT_PLAN_MODEL", default=""),
            agent_implement_model=_env("AGENT_IMPLEMENT_MODEL", default=""),
            agent_review_model=_env("AGENT_REVIEW_MODEL", default=""),
            # Difficulty-routed implement tiers (blank = no swap). See do_implement.
            agent_implement_model_easy=_env("AGENT_IMPLEMENT_MODEL_EASY", default=""),
            agent_implement_model_hard=_env("AGENT_IMPLEMENT_MODEL_HARD", default=""),
            # opencode-only: a stronger model to escalate to after the primary
            # fails a run with a transient provider error (overloaded/503). Empty
            # disables escalation. Ignored by the Claude runner. Kept for back-compat;
            # AGENT_FALLBACK_MODELS (plural) is the preferred way to list several.
            agent_fallback_model=_env("AGENT_FALLBACK_MODEL", default=""),
            # opencode-only: an ordered, comma-separated list of fallback models to
            # try (each a distinct attempt) after the primary, before the ticket is
            # handed back. The singular AGENT_FALLBACK_MODEL is appended so existing
            # configs keep working. e.g. AGENT_FALLBACK_MODELS=google/gemini-2.0-flash,
            # google/gemini-2.5-pro
            agent_fallback_models=_split_models(
                _env("AGENT_FALLBACK_MODELS", default=""),
                _env("AGENT_FALLBACK_MODEL", default="")),
            implement_tools=_env(
                "AGENT_IMPLEMENT_TOOLS", "CLAUDE_IMPLEMENT_TOOLS",
                # Broad Bash so the agent can run tests/build in the worktree;
                # isolation comes from the worktree + PROJECTS_ROOT allowlist,
                # not from narrowing Bash (compound `cd && cmd` defeats that).
                # Claude-tool-name allowlist; the opencode runner ignores this.
                default="Edit Write Read Glob Grep Bash",
            ),
            agent_timeout=int(_env("AGENT_TIMEOUT", "CLAUDE_TIMEOUT", default="1800")),
            # The implement phase does strictly more than plan (edit + verify),
            # so give it a larger default budget than the (read-only) plan phase.
            agent_implement_timeout=int(
                _env("AGENT_IMPLEMENT_TIMEOUT", "CLAUDE_IMPLEMENT_TIMEOUT", default="2400")
            ),
            # Read-only plan/review cap. Much shorter than implement (and shorter than
            # the legacy AGENT_TIMEOUT) so a stalled model is given up on quickly and
            # the fallback chain moves on. Defaults to AGENT_TIMEOUT when set (so old
            # configs that tuned it keep working), else 600s.
            agent_plan_review_timeout=int(
                _env("AGENT_PLAN_REVIEW_TIMEOUT", "AGENT_TIMEOUT", "CLAUDE_TIMEOUT",
                     default="600")
            ),
            # opencode named agents: "plan" is permission-restricted (no edit/bash)
            # for the read-only plan phase; "build" is unrestricted for implement.
            opencode_plan_agent=_env("OPENCODE_PLAN_AGENT", default="plan"),
            opencode_build_agent=_env("OPENCODE_BUILD_AGENT", default="build"),
            patch_fallback=os.environ.get("PATCH_FALLBACK", "0").strip() in ("1", "true", "yes"),
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
            # Delete archived logs older than this many days at sweep start.
            log_retention_days=int(os.environ.get("LOG_RETENTION_DAYS", "14")),
            # Roll loose logs from days older than this into daily tarballs. 1 =
            # keep today loose, archive yesterday and older.
            log_archive_after_days=int(os.environ.get("LOG_ARCHIVE_AFTER_DAYS", "1")),
            # This bot's own cron stdout log, size-rotated at sweep start. Each
            # identity points CRON_LOG at its own file (e.g. cron.log vs
            # cron-gemini.log); unset disables rotation (back-compat).
            cron_log=_cron_log_path(),
            cron_log_max_bytes=int(os.environ.get("CRON_LOG_MAX_BYTES", "5000000")),
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
            # Difficulty routing: the free bot hands hard/important tickets to this
            # user (the Claude bot). 0 = disabled.
            escalate_to_user_id=int(os.environ.get("ESCALATE_TO_USER_ID", "0") or "0"),
            escalate_priorities=_split_models(
                _env("ESCALATE_PRIORITIES", default="high,critical")),
            # Who a `/consolidate` run's review ticket is assigned to. Default 4
            # (claude-lite-ubvm), matching the ticket's explicit ask.
            consolidate_review_user_id=int(
                os.environ.get("CONSOLIDATE_REVIEW_USER_ID", "4") or "4"),
            # Single-shot review backend (direct OpenAI-compatible chat completion).
            review_api_url=_env("REVIEW_API_URL", default=""),
            review_api_key=_env("REVIEW_API_KEY", default=""),
            review_api_model=_env("REVIEW_API_MODEL", default=""),
            # Plan-critique gate (direct OpenAI-compatible chat completion). All three
            # set ⇒ gate on; a REVISE verdict re-plans up to CRITIQUE_MAX_REVISIONS times.
            critique_api_url=_env("CRITIQUE_API_URL", default=""),
            critique_api_key=_env("CRITIQUE_API_KEY", default=""),
            critique_api_model=_env("CRITIQUE_API_MODEL", default=""),
            critique_max_revisions=int(os.environ.get("CRITIQUE_MAX_REVISIONS", "1")),
            # Verification gate. VERIFY_COMMAND is a shell string run in the worktree
            # (e.g. `cd backend && .venv/bin/pytest -q`); empty disables the gate. Note
            # the worktree is a FRESH checkout with no gitignored .venv/node_modules, so
            # the command must be self-contained. On failure the resolver re-invokes the
            # implement agent with the output up to VERIFY_MAX_RETRIES times, then
            # publishes flagged.
            verify_command=_env("VERIFY_COMMAND", default=""),
            verify_timeout=int(os.environ.get("VERIFY_TIMEOUT", "900")),
            verify_max_retries=int(os.environ.get("VERIFY_MAX_RETRIES", "1")),
            # How long to wait after a quota/rate-limit failure before auto-retrying
            # the parked ticket from the same phase (see quota_backoff). Default 60m.
            quota_backoff_minutes=int(os.environ.get("QUOTA_BACKOFF_MINUTES", "60")),
            # Resolver-to-resolver delegation (fan-out). Opt-in: off unless explicitly
            # enabled AND a worker roster is configured (see _parse_workers).
            allow_delegation=os.environ.get("RESOLVER_ALLOW_DELEGATION", "0").strip()
            in ("1", "true", "yes"),
            workers=_parse_workers(os.environ.get("RESOLVER_WORKERS", "")),
            max_delegations=int(os.environ.get("RESOLVER_MAX_DELEGATIONS", "10")),
            # Daily digest. DIGEST_ADMIN_KEY must belong to an *admin* user (see the
            # field comment); the API trio falls back to REVIEW_API_* in digest.py so
            # a resolver that already has a cheap chat model needs no extra config.
            digest_admin_key=_env("DIGEST_ADMIN_KEY", default=""),
            digest_api_url=_env("DIGEST_API_URL", default=""),
            digest_api_key=_env("DIGEST_API_KEY", default=""),
            digest_api_model=_env("DIGEST_API_MODEL", default=""),
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
