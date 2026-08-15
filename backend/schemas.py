"""Pydantic request/response schemas."""
import re
from datetime import datetime, timezone
from typing import Annotated, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, PlainSerializer, field_validator

from control_tags import ALL_SCOPES
from models import (
    NotificationChannel,
    NotificationType,
    TicketPriority,
    TicketStatus,
    TicketType,
    UserRole,
)

# --- Tag validation ----------------------------------------------------------
# Defense in depth, applied to ALL callers (including the resolver bot). Tag
# strings are concatenated into LLM prompts, so newlines / control characters
# are a prompt-injection vector; we also bound count and length. The charset is
# permissive enough that existing control tags (``claude:…``, ``repo:…``) still
# validate.

MAX_TAGS = 30
MAX_TAG_LENGTH = 50
# Letters, digits and a small set of punctuation used by real tags
# (`claude:planning`, `repo:my-app`, `c++`, `area/backend`, etc.). Notably
# excludes whitespace control chars like \n, \r, \t.
_TAG_CHARS = re.compile(r"^[\w:./+\-# ]+$")


def _clean_tags(tags: Optional[List[str]]) -> Optional[List[str]]:
    if tags is None:
        return None
    cleaned: List[str] = []
    seen = set()
    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError("tags must be strings")
        tag = tag.strip()
        if not tag:
            continue  # drop empties
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"tag too long (max {MAX_TAG_LENGTH} chars): {tag!r}")
        if not _TAG_CHARS.match(tag):
            raise ValueError(f"tag contains invalid characters: {tag!r}")
        if tag not in seen:
            seen.add(tag)
            cleaned.append(tag)
    if len(cleaned) > MAX_TAGS:
        raise ValueError(f"too many tags (max {MAX_TAGS})")
    return cleaned

# --- Datetime serialization --------------------------------------------------
# DB datetimes are stored as UTC but come back naive (SQLite has no tz type, and
# the columns are plain DateTime). Serialize them as UTC-aware ISO-8601 so every
# consumer (frontend, bot, curl) gets an explicit offset instead of an ambiguous
# timezone-less string that browsers reinterpret as local time.

def _as_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


UTCDateTime = Annotated[datetime, PlainSerializer(_as_utc_iso, return_type=str)]

# --- Users -------------------------------------------------------------------

class UserPublic(BaseModel):
    """User shape returned to other users (no secrets)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    email: str
    role: UserRole
    is_resolver_bot: bool = False
    created_at: UTCDateTime


class UserSelf(UserPublic):
    """User shape returned to the user themselves.

    API keys are managed separately (see the ApiKey* schemas) and never embedded
    here — only their hash is ever stored server-side.
    """


class UserCreate(BaseModel):
    username: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    email: EmailStr
    password: str = Field(min_length=6)
    role: UserRole = UserRole.member


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(default=None, min_length=6)
    role: Optional[UserRole] = None  # only honored for admin callers


class ResolverBotCreate(BaseModel):
    """Provision a resolver bot (a least-privilege member flagged
    ``is_resolver_bot``) plus its first API key in one admin call. Used by the
    ``resolver`` CLI so operators don't hand-create bots or sync ids."""
    username: str = Field(min_length=1)
    display_name: Optional[str] = None
    email: Optional[EmailStr] = None


class ResolverBotCreated(BaseModel):
    """Returned exactly once: the new bot's id and its raw API key."""
    user_id: int
    username: str
    api_key: str


# --- Auth --------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


# --- Code blocks -------------------------------------------------------------

class CodeBlock(BaseModel):
    filename: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    content: str
    language: str = "plaintext"


# --- Tickets -----------------------------------------------------------------

class TicketCreate(BaseModel):
    type: TicketType
    title: str = Field(min_length=1)
    description: str = ""
    priority: TicketPriority = TicketPriority.medium
    status: TicketStatus = TicketStatus.open
    assigned_to: Optional[int] = None
    due_date: Optional[datetime] = None
    code_blocks: List[CodeBlock] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v):
        return _clean_tags(v)


class TicketUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[TicketStatus] = None
    priority: Optional[TicketPriority] = None
    assigned_to: Optional[int] = None
    due_date: Optional[datetime] = None
    code_blocks: Optional[List[CodeBlock]] = None
    tags: Optional[List[str]] = None

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v):
        return _clean_tags(v)


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: TicketType
    title: str
    description: str
    status: TicketStatus
    priority: TicketPriority
    archived: bool
    created_by: int
    assigned_to: Optional[int]
    created_at: UTCDateTime
    updated_at: UTCDateTime
    due_date: Optional[UTCDateTime]
    code_blocks: List[CodeBlock]
    tags: List[str]


# --- Comments ----------------------------------------------------------------

class CommentCreate(BaseModel):
    body: str = Field(min_length=1)


class CommentUpdate(BaseModel):
    body: str = Field(min_length=1)


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    author: int
    body: str
    created_at: UTCDateTime


# --- Activity ----------------------------------------------------------------

class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    actor: Optional[int] = Field(default=None, validation_alias="actor_id")
    action: str
    detail: Optional[dict] = None
    created_at: UTCDateTime


# --- Agent runs --------------------------------------------------------------
# Allowed vocabularies are constrained via Literal so the resolver (or any caller)
# can't write garbage phase/agent/status values — bad input is rejected with 422.

# "review-api"/"critique-api" are the direct chat-completion backends (no agent loop):
# single-shot reviews and the plan-critique gate. They POST runs like the agents do.
AgentName = Literal["claude", "opencode", "review-api", "critique-api"]
AgentPhaseName = Literal["plan", "implement", "review", "plan-critique"]
AgentRunStatusName = Literal["succeeded", "failed"]


class AgentRunCreate(BaseModel):
    agent: AgentName
    phase: AgentPhaseName
    model: str = ""
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    status: AgentRunStatusName = "succeeded"
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    agent: str
    phase: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    status: str
    started_at: Optional[UTCDateTime] = None
    finished_at: UTCDateTime
    created_at: UTCDateTime


class AgentRunTotals(BaseModel):
    """Summed token usage + cost over a set of agent runs."""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    run_count: int = 0


class CostRollupChild(BaseModel):
    ticket_id: int
    title: str
    totals: AgentRunTotals


class CostRollup(BaseModel):
    """A ticket's own agent-run cost plus the cost of every delegated child
    (tickets tagged ``parent:<id>``), and the combined total."""
    ticket_id: int
    own: AgentRunTotals
    children: List[CostRollupChild]
    total: AgentRunTotals


# --- Notifications -----------------------------------------------------------

class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    ticket_id: Optional[int] = None
    ticket_title: str
    actor_id: Optional[int] = None
    actor_name: str
    comment_id: Optional[int] = None
    read: bool
    created_at: UTCDateTime


class NotificationList(BaseModel):
    items: List[NotificationOut]
    total: int
    unread_count: int
    limit: int
    offset: int


class UnreadCount(BaseModel):
    unread_count: int


class BulkDeleteRequest(BaseModel):
    ids: List[int] = Field(default_factory=list)
    all: bool = False


# --- Notification preferences ------------------------------------------------

class NotificationPreferenceItem(BaseModel):
    """One toggle in the settings matrix: (type, channel) -> enabled."""
    type: NotificationType
    channel: NotificationChannel
    enabled: bool


class NotificationPreferences(BaseModel):
    """The full per-user matrix (every type x channel), defaults filled in."""
    items: List[NotificationPreferenceItem]


class NotificationPreferencesUpdate(BaseModel):
    items: List[NotificationPreferenceItem] = Field(default_factory=list)


# --- Pagination --------------------------------------------------------------

class PaginatedTickets(BaseModel):
    items: List[TicketOut]
    total: int
    limit: int
    offset: int


# --- API keys ----------------------------------------------------------------

class ApiKeyMeta(BaseModel):
    """Non-secret metadata about an API key (safe to list)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key_prefix: str
    created_at: UTCDateTime
    last_used_at: Optional[UTCDateTime] = None
    expires_at: Optional[UTCDateTime] = None
    revoked: bool
    scopes: list[str] = []

    @field_validator("scopes", mode="before")
    @classmethod
    def _split_scopes(cls, v):
        """The column is comma-separated; expose it as a list."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v or []


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1)
    expires_in_days: Optional[int] = Field(default=None, ge=1)
    # Capability grants for this key (currently only "cli", which permits repo:
    # tags). Admin-only — enforced in routers/users.create_api_key.
    scopes: list[str] = Field(default_factory=list)

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, v):
        unknown = set(v) - ALL_SCOPES
        if unknown:
            raise ValueError(f"unknown scopes: {sorted(unknown)}")
        return sorted(set(v))


class ApiKeyCreated(ApiKeyMeta):
    """Returned exactly once on creation — includes the plaintext key."""
    api_key: str


# --- Resolver settings -------------------------------------------------------
# Server-managed, NON-SECRET tunables for the resolver daemon (see the
# ResolverSettings model). Secrets (STINGRAY_API_KEY, REVIEW_API_KEY,
# CRITIQUE_API_KEY, provider keys) are deliberately absent: they stay in the
# resolver's .env and are only described (never valued) via SecretField below.


class ResolverWorker(BaseModel):
    """One entry in the delegation roster (mirrors config._parse_workers)."""
    id: int
    name: str
    desc: str = ""


class ResolverSettingsValues(BaseModel):
    """The full non-secret tunable surface, with defaults matching config.py.

    GET returns a complete view (defaults <- global row <- bot row), so the UI
    always renders every field even when nothing is stored yet.
    """
    agent_model: str = ""
    agent_plan_model: str = ""
    agent_implement_model: str = ""
    agent_review_model: str = ""
    agent_implement_model_easy: str = ""
    agent_implement_model_hard: str = ""
    agent_fallback_models: List[str] = Field(default_factory=list)
    escalate_to_user_id: int = 0
    escalate_priorities: List[str] = Field(default_factory=lambda: ["high", "critical"])
    max_attempts: int = 3
    max_tickets_per_sweep: int = 0
    verify_command: str = ""
    verify_timeout: int = 900
    verify_max_retries: int = 1
    critique_max_revisions: int = 1
    quota_backoff_minutes: int = 60
    allow_delegation: bool = False
    max_delegations: int = 10
    default_repo: str = ""
    repo_map: Dict[str, str] = Field(default_factory=dict)
    workers: List[ResolverWorker] = Field(default_factory=list)
    audit_output_tail_bytes: int = 4096


class ResolverSettingsUpdate(BaseModel):
    """Admin write payload. ``extra="forbid"`` rejects unknown or secret keys
    (e.g. ``stingray_api_key``) with 422 — secrets can never be written here.
    Every field is optional; only the ones sent are stored/overridden."""
    model_config = ConfigDict(extra="forbid")

    agent_model: Optional[str] = None
    agent_plan_model: Optional[str] = None
    agent_implement_model: Optional[str] = None
    agent_review_model: Optional[str] = None
    agent_implement_model_easy: Optional[str] = None
    agent_implement_model_hard: Optional[str] = None
    agent_fallback_models: Optional[List[str]] = None
    escalate_to_user_id: Optional[int] = Field(default=None, ge=0)
    escalate_priorities: Optional[List[str]] = None
    max_attempts: Optional[int] = Field(default=None, ge=1)
    max_tickets_per_sweep: Optional[int] = Field(default=None, ge=0)
    verify_command: Optional[str] = None
    verify_timeout: Optional[int] = Field(default=None, ge=1)
    verify_max_retries: Optional[int] = Field(default=None, ge=0)
    critique_max_revisions: Optional[int] = Field(default=None, ge=0)
    quota_backoff_minutes: Optional[int] = Field(default=None, ge=0)
    allow_delegation: Optional[bool] = None
    max_delegations: Optional[int] = Field(default=None, ge=0)
    default_repo: Optional[str] = None
    repo_map: Optional[Dict[str, str]] = None
    workers: Optional[List[ResolverWorker]] = None
    audit_output_tail_bytes: Optional[int] = Field(default=None, ge=0)


class SecretField(BaseModel):
    """A read-only descriptor for a resolver secret. Carries NO value — the
    backend never holds the resolver's .env, so there is nothing to leak. The
    UI renders these as disabled '•••• managed in .env' rows."""
    name: str
    label: str
    managed_in: str = ".env"


class ResolverSettingsOut(BaseModel):
    bot_user_id: Optional[int] = None
    settings: ResolverSettingsValues
    secrets: List[SecretField]
    updated_at: Optional[UTCDateTime] = None
    updated_by: Optional[int] = None


# --- Resolver registry (the live manager) ------------------------------------
# A running resolver reports its identity + observed state each sweep. Distinct
# from ResolverSettings (admin overrides); this is what the resolver says about
# itself. Non-secret only, same guarantees as above.


class ResolverHeartbeat(BaseModel):
    """A resolver's per-sweep self-report. ``extra="forbid"`` rejects any secret
    or unknown key; ``effective_config`` reuses the non-secret tunable set so a
    snapshot can never carry a secret."""
    model_config = ConfigDict(extra="forbid")

    label: str = ""       # RESOLVER_ENV_FILE, e.g. ".env.gemini"
    name: str = ""        # clean identity name, e.g. "gemini"
    agent: str = ""       # claude | opencode | ...
    model: str = ""
    effective_config: ResolverSettingsValues = Field(default_factory=ResolverSettingsValues)


class ResolverRosterEntry(BaseModel):
    """One row in the resolver-manager roster: the bot's identity plus its live
    self-reported state (null until it first sweeps)."""
    bot_user_id: int
    username: str
    display_name: str
    is_bot: bool = True
    has_settings: bool = False
    # Live fields — null when the resolver has never sent a heartbeat.
    name: Optional[str] = None
    label: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    last_seen_at: Optional[UTCDateTime] = None
    effective_config: Optional[ResolverSettingsValues] = None
