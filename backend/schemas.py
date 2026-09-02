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
    WebhookEventType,
)
from webhook_urls import validate_webhook_url

# --- Tag validation ----------------------------------------------------------
# Defense in depth, applied to ALL callers (including the resolver bot). Tag
# strings are concatenated into LLM prompts, so newlines / control characters
# are a prompt-injection vector; we also bound count and length. The charset is
# permissive enough that existing control tags (``claude:…``, ``repo:…``) still
# validate.

MAX_TAGS = 30
MAX_TAG_LENGTH = 50

# --- Credential bounds -------------------------------------------------------
# Applied to both account creation and login. Unbounded credential fields are a
# cheap DoS: an unauthenticated login stores the submitted username as a key in
# the in-memory lockout map, and bcrypt hashes whatever password it is handed.
MAX_USERNAME_LENGTH = 64
MAX_PASSWORD_LENGTH = 128
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
    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH)
    display_name: str = Field(min_length=1)
    email: EmailStr
    password: str = Field(min_length=6, max_length=MAX_PASSWORD_LENGTH)
    role: UserRole = UserRole.member


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=MAX_PASSWORD_LENGTH)
    role: Optional[UserRole] = None  # only honored for admin callers


class ResolverBotCreate(BaseModel):
    """Provision a resolver bot (a least-privilege member flagged
    ``is_resolver_bot``) plus its first API key in one admin call. Used by the
    ``resolver`` CLI so operators don't hand-create bots or sync ids."""
    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH)
    display_name: Optional[str] = None
    email: Optional[EmailStr] = None


class ResolverBotCreated(BaseModel):
    """Returned exactly once: the new bot's id and its raw API key."""
    user_id: int
    username: str
    api_key: str


# --- Auth --------------------------------------------------------------------

class LoginRequest(BaseModel):
    # Bounded on purpose: a failed login stores the submitted username as a key
    # in the in-memory lockout map (login_throttle.py) even when no such user
    # exists, so an unbounded field would let anyone plant megabyte-sized keys.
    # Rejecting here also skips the bcrypt verify for junk input. The limits
    # match UserCreate, so no account that can be created is locked out by them.
    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


# --- Code blocks -------------------------------------------------------------

class CodeBlock(BaseModel):
    filename: str = Field(max_length=300, pattern=r'^[^\x00-\x1f]*$')
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    content: str = Field(max_length=102_400)
    language: str = Field(default="plaintext", max_length=30, pattern=r'^[a-zA-Z0-9_-]*$')


# --- Tickets -----------------------------------------------------------------

class TicketCreate(BaseModel):
    type: TicketType
    title: str = Field(min_length=1)
    description: str = ""
    priority: TicketPriority = TicketPriority.medium
    status: TicketStatus = TicketStatus.open
    assigned_to: Optional[int] = None
    due_date: Optional[datetime] = None
    code_blocks: List[CodeBlock] = Field(default_factory=list, max_length=20)
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


class PaginatedComments(BaseModel):
    items: List[CommentOut]
    total: int
    limit: int
    offset: int


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
    # The tail of a FAILED run's transcript, already redacted by the resolver.
    # Capped here as well as there: this is the one field on an agent run whose
    # size is set by how chatty an agent was rather than by the schema, and the
    # sender is a bot posting unattended.
    log_tail: str = Field(default="", max_length=20_000)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    # Optional proof that the poster still holds the ticket's lease. Omitting it
    # keeps the pre-lease behavior (the assignee gate alone), so existing callers
    # are unaffected; supplying it makes the write fail once the lease has lapsed,
    # which is what stops a worker that was presumed dead — and whose ticket has
    # since been re-claimed — from writing results over its replacement's.
    lease_token: Optional[str] = Field(default=None, max_length=100)


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
    # Returned to anyone who may view the ticket — the same gate the rest of the
    # run is behind, and the same gate that already governs code_blocks, which
    # carry private source.
    log_tail: str = ""
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


# --- Ticket leases -----------------------------------------------------------
# Bounds on a lease's lifetime. The floor keeps a claim from expiring before the
# holder's first heartbeat can land; the ceiling keeps a crashed worker from
# stranding a ticket for hours, which is the whole point of putting a TTL on the
# claim in the first place.
MIN_LEASE_TTL = 5
MAX_LEASE_TTL = 3600
DEFAULT_LEASE_TTL = 300


class ClaimRequest(BaseModel):
    """How long the claimant wants the lease for. Workers extend rather than
    asking for a long TTL up front — see :class:`models.TicketLease`."""
    ttl_seconds: int = Field(default=DEFAULT_LEASE_TTL, ge=MIN_LEASE_TTL, le=MAX_LEASE_TTL)


class LeaseRelease(BaseModel):
    """Proof of holding the lease being dropped."""
    token: str = Field(min_length=1, max_length=100)


class LeaseExtend(LeaseRelease):
    ttl_seconds: int = Field(default=DEFAULT_LEASE_TTL, ge=MIN_LEASE_TTL, le=MAX_LEASE_TTL)


class LeaseOut(BaseModel):
    """A granted lease. ``token`` is returned only to the worker that claimed
    (or extended) it; there is no endpoint that hands it to anyone else."""
    model_config = ConfigDict(from_attributes=True)

    ticket_id: int
    worker_id: int
    token: str
    expires_at: UTCDateTime


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


# --- Tag facets --------------------------------------------------------------

class TagFacet(BaseModel):
    """One tag and how many visible tickets carry it (drives the tag picker)."""
    tag: str
    count: int


class TagFacets(BaseModel):
    items: List[TagFacet]


# --- Saved views -------------------------------------------------------------
# A saved view is a named dashboard query string. `query` is opaque to the
# backend (never parsed or executed), but it is still bounded and charset-checked:
# it is echoed back into a client that pushes it into the URL, so unbounded or
# control-character content would be both a storage and an injection concern.

MAX_SAVED_VIEWS = 50
MAX_VIEW_NAME_LENGTH = 60
MAX_VIEW_QUERY_LENGTH = 1000

# Printable ASCII only — the character set a URL query string is built from.
_QUERY_CHARS = re.compile(r"^[\w:./+\-#%&=,\[\]~!$'()*; ]*$")


def _clean_view_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("name must not be empty")
    if len(name) > MAX_VIEW_NAME_LENGTH:
        raise ValueError(f"name too long (max {MAX_VIEW_NAME_LENGTH} chars)")
    if any(ord(c) < 32 for c in name):
        raise ValueError("name must not contain control characters")
    return name


def _clean_view_query(query: str) -> str:
    # A leading "?" is what `location.search` hands you; accept and drop it so
    # the stored form is always the bare query string.
    query = query.strip().lstrip("?")
    if len(query) > MAX_VIEW_QUERY_LENGTH:
        raise ValueError(f"query too long (max {MAX_VIEW_QUERY_LENGTH} chars)")
    if not _QUERY_CHARS.match(query):
        raise ValueError("query contains invalid characters")
    return query


class SavedViewCreate(BaseModel):
    name: str
    query: str = ""

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        return _clean_view_name(v)

    @field_validator("query")
    @classmethod
    def _v_query(cls, v: str) -> str:
        return _clean_view_query(v)


class SavedViewUpdate(BaseModel):
    """Partial update: omit a field to leave it alone."""
    name: Optional[str] = None
    query: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _clean_view_name(v)

    @field_validator("query")
    @classmethod
    def _v_query(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _clean_view_query(v)


class SavedViewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    query: str
    created_at: UTCDateTime
    updated_at: UTCDateTime


# --- Webhooks ----------------------------------------------------------------
# A webhook is an outbound request the server makes to a user-supplied URL, so
# the URL is validated *here*, at the schema layer: a rejected URL is a 422 with
# the specific reason, and the router cannot forget to call the check. See
# webhook_urls for the SSRF rules themselves.

MAX_WEBHOOKS_PER_USER = 20
MAX_WEBHOOK_NAME_LENGTH = 60
MAX_TAG_FILTERS = 20


def _clean_webhook_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("name must not be empty")
    if len(name) > MAX_WEBHOOK_NAME_LENGTH:
        raise ValueError(f"name too long (max {MAX_WEBHOOK_NAME_LENGTH} chars)")
    if any(ord(c) < 32 for c in name):
        raise ValueError("name must not contain control characters")
    return name


def _clean_tag_filter(tags: List[str]) -> List[str]:
    """Reuse the ticket tag rules — a filter only ever matches real tags."""
    cleaned = _clean_tags(tags) or []
    if len(cleaned) > MAX_TAG_FILTERS:
        raise ValueError(f"too many tag filters (max {MAX_TAG_FILTERS})")
    return cleaned


class WebhookCreate(BaseModel):
    name: str
    url: str
    # Empty list = subscribe to every event type.
    event_types: List[WebhookEventType] = Field(default_factory=list)
    tag_filter: List[str] = Field(default_factory=list)
    active: bool = True

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        return _clean_webhook_name(v)

    @field_validator("url")
    @classmethod
    def _v_url(cls, v: str) -> str:
        return validate_webhook_url(v)

    @field_validator("tag_filter")
    @classmethod
    def _v_tags(cls, v: List[str]) -> List[str]:
        return _clean_tag_filter(v)


class WebhookUpdate(BaseModel):
    """Partial update: omit a field to leave it alone."""
    name: Optional[str] = None
    url: Optional[str] = None
    event_types: Optional[List[WebhookEventType]] = None
    tag_filter: Optional[List[str]] = None
    active: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _clean_webhook_name(v)

    @field_validator("url")
    @classmethod
    def _v_url(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else validate_webhook_url(v)

    @field_validator("tag_filter")
    @classmethod
    def _v_tags(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return None if v is None else _clean_tag_filter(v)


class WebhookOut(BaseModel):
    """The webhook as every read path returns it — **no `secret` field.**

    Adding one here is the whole bug this feature has to avoid; the plaintext
    secret is exposed only by WebhookCreated / WebhookSecretRotated.
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    url: str
    event_types: List[str] = []
    tag_filter: List[str] = []
    active: bool
    consecutive_failures: int
    secret_prefix: str
    created_at: UTCDateTime
    updated_at: UTCDateTime


class WebhookCreated(WebhookOut):
    """Returned exactly once on creation — includes the plaintext secret."""
    secret: str


class WebhookSecretRotated(BaseModel):
    """Returned exactly once on rotation — includes the new plaintext secret."""
    id: int
    secret: str
    secret_prefix: str


class WebhookDeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    webhook_id: int
    event_id: Optional[int] = None
    event_type: str
    ticket_id: Optional[int] = None
    attempt_count: int
    next_attempt_at: Optional[UTCDateTime] = None
    status_code: Optional[int] = None
    response_snippet: str
    error: str
    state: str
    created_at: UTCDateTime
    updated_at: UTCDateTime


class PaginatedDeliveries(BaseModel):
    items: List[WebhookDeliveryOut]
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
    # Capability grants for this key: "cli" (permits the repo:/rev:/branch: aiming
    # tags) or "agent" (permits the parent:/review-by: routing tags and registering
    # in the agent registry). Admin-only — enforced in routers/users.create_api_key.
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


class AgentHeartbeat(BaseModel):
    """A third-party agent's self-report. Same shape as ``ResolverHeartbeat``,
    except ``effective_config`` is a free-form dict: an external worker's config
    is its own, not our resolver's tunable set. ``extra="forbid"`` still rejects
    unknown top-level keys, and callers are reminded here as in the resolver
    case that this row is world-readable to admins — **no secrets**."""
    model_config = ConfigDict(extra="forbid")

    label: str = ""       # deployment label, e.g. "prod-us-east"
    name: str = ""        # clean identity name, e.g. "triage-bot"
    agent: str = ""       # the worker's own agent/runtime name
    model: str = ""
    effective_config: dict = Field(default_factory=dict)


class AgentRosterEntry(BaseModel):
    """One row in the agent registry: every worker that has ever sent a
    heartbeat, ours and third-party alike."""
    user_id: int
    username: str
    display_name: str
    is_resolver_bot: bool = False
    name: Optional[str] = None
    label: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    last_seen_at: Optional[UTCDateTime] = None


# --- Chat assistant ----------------------------------------------------------

# Bounds on the question. Unbounded free text here is both a cost problem (it is
# forwarded to a metered provider) and a context problem (it competes with the
# ticket context for room), so it is capped well below the context budget.
MAX_QUESTION_LENGTH = 4000


class ChatConfigOut(BaseModel):
    """What the browser is told about the assistant's configuration.

    Deliberately excludes the endpoint URL and key, which live in the backend's
    environment and are never exposed (see ``chat/config.py``). The UI renders no
    trace of the feature when ``enabled`` is false.

    The spend fields are per-caller, not deployment-wide: they let the popup show
    "$0.12 of $0.50 today" without a second request. ``daily_usd_limit`` is 0.0
    when no cap is configured.
    """
    enabled: bool
    model: str = ""
    daily_usd_limit: float = 0.0
    spent_today_usd: float = 0.0


class ChatAskRequest(BaseModel):
    """One question, optionally anchored to a ticket.

    ``ticket_id`` is a *request* for context, not an authorization claim: the
    router resolves it against the caller's own read permissions and 404s when
    they may not see it.
    """
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    ticket_id: Optional[int] = None

    @field_validator("question")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be blank")
        return v


class ChatUsage(BaseModel):
    """Token usage and cost for one answer.

    Mirrors ``AgentRunOut``'s accounting fields on purpose: resolver work and
    chat work are both metered AI spend, and the UI should be able to render
    them the same way.
    """
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ChatAskResponse(BaseModel):
    answer: str
    usage: ChatUsage
    # Which ticket's context actually went into the answer, and how much of it —
    # so the UI can show what the assistant was looking at, and a truncated pack
    # is visible rather than silent.
    context_ticket_id: Optional[int] = None
    context_chars: int = 0


class ChatMessageOut(BaseModel):
    """One stored turn. Assistant turns carry the accounting; user turns don't."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    meta: Dict = Field(default_factory=dict)
    created_at: UTCDateTime


class ChatConversationSummary(BaseModel):
    """A thread as it appears in the popup's thread list — no message bodies."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    ticket_id: Optional[int] = None
    created_at: UTCDateTime
    updated_at: UTCDateTime


class ChatConversationOut(ChatConversationSummary):
    """A thread with its full transcript, oldest first."""
    messages: List[ChatMessageOut] = Field(default_factory=list)


class ChatConversationCreate(BaseModel):
    """``ticket_id`` anchors the thread to a ticket. It is validated against the
    caller's own read permission at creation *and* re-checked on every turn, so
    it never becomes a stored grant of access."""
    ticket_id: Optional[int] = None


class ChatSendRequest(BaseModel):
    """One question in an existing thread.

    ``ticket_id`` overrides the thread's anchor for this turn only — the popup
    sends the ticket the user is currently looking at, which may differ from the
    one the thread started on.
    """
    content: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    ticket_id: Optional[int] = None

    @field_validator("content")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("content must not be blank")
        return v
