"""SQLAlchemy ORM models: User, Ticket, Comment."""
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship, synonym

from database import Base


def utcnow():
    return datetime.now(timezone.utc)


# --- Enumerations (values are stored as plain strings in SQLite) -------------

class UserRole(str, enum.Enum):
    admin = "admin"
    member = "member"


class TicketType(str, enum.Enum):
    code_review = "code_review"
    task = "task"


class TicketStatus(str, enum.Enum):
    open = "open"
    in_review = "in_review"
    changes_requested = "changes_requested"
    resolved = "resolved"
    closed = "closed"


class TicketPriority(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


# Sort rank for `priority`. The column is a String, so ordering by it directly
# would be alphabetical ("critical" < "high" < "low" < "medium") — meaningless.
# Lives here, next to the enum, so the two can't drift; routers.tickets turns it
# into a SQL CASE for `?sort=priority`.
PRIORITY_ORDER: dict[str, int] = {
    TicketPriority.critical.value: 0,
    TicketPriority.high.value: 1,
    TicketPriority.medium.value: 2,
    TicketPriority.low.value: 3,
}


class NotificationType(str, enum.Enum):
    assigned = "assigned"
    commented = "commented"
    # Not caused by another user: the dispatcher raises this against a webhook's
    # owner when a run of failures auto-disables it (see dispatcher.py). It is
    # the one notification type that names no ticket and no actor.
    webhook_disabled = "webhook_disabled"


class NotificationChannel(str, enum.Enum):
    """Where a notification is delivered. ``in_app`` is the bell/inbox;
    ``email`` is the SMTP path in ``notifications.py``; ``page_title`` updates
    the browser tab with an unread-count indicator."""
    in_app = "in_app"
    email = "email"
    page_title = "page_title"


class AgentPhase(str, enum.Enum):
    plan = "plan"
    implement = "implement"
    review = "review"


class AgentRunStatus(str, enum.Enum):
    succeeded = "succeeded"
    failed = "failed"


class WebhookEventType(str, enum.Enum):
    """Event types a webhook may subscribe to.

    These are exactly the ``type`` values ``events.emit`` writes to the outbox —
    a subscription to something that is never emitted would silently never fire,
    so the two lists must stay in step.
    """
    ticket_created = "ticket.created"
    ticket_assigned = "ticket.assigned"
    ticket_status_changed = "ticket.status_changed"
    ticket_tagged = "ticket.tagged"
    comment_created = "comment.created"
    agent_run_finished = "agent_run.finished"


class DeliveryState(str, enum.Enum):
    pending = "pending"        # queued, awaiting its (first or next) attempt
    delivering = "delivering"  # claimed by the delivery worker
    succeeded = "succeeded"
    failed = "failed"          # retries exhausted
    skipped = "skipped"        # never sent, e.g. the URL failed re-validation

# Response bodies are recorded for debugging, not stored wholesale: a receiver
# that answers with a megabyte of HTML would otherwise fill the log.
MAX_RESPONSE_SNIPPET = 2000


# --- Models ------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    role = Column(String, nullable=False, default=UserRole.member.value)
    hashed_password = Column(String, nullable=False)
    # Trusted automation identity: a resolver bot may set the reserved control
    # tags (claude:*, repo:*, dangerous, fix, delegate) without being an admin.
    # Set at seed time (see seed.seed_resolver_bot) so the trust is recorded in
    # the DB instead of a RESOLVER_BOT_USER_ID env var that must be kept in sync.
    is_resolver_bot = Column(Boolean, nullable=False, default=False)
    # Bumped whenever existing sessions must be invalidated (logout, password
    # change, role change). Embedded in the session token and checked on every
    # request, so a stale/leaked cookie stops working once this changes.
    session_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    api_keys = relationship(
        "ApiKey", back_populates="user", cascade="all, delete-orphan"
    )


class ApiKey(Base):
    """A named, hashed API key. Multiple per user; supports expiry + revocation.

    Only a sha256 hash of the key is stored — the plaintext is shown exactly once,
    at creation time. `key_prefix` keeps a short, non-secret label for display.
    """
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    key_prefix = Column(String, nullable=False)
    key_hash = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    revoked = Column(Boolean, nullable=False, default=False)
    # Comma-separated capability grants, e.g. "cli". A key's authority is normally
    # its owner's; a scope widens it in one narrow, named way (see control_tags
    # .SCOPE_TAG_PREFIXES). Stored as a string rather than JSON so the migration is
    # a plain ADD COLUMN with a scalar default. Only an admin may grant one.
    scopes = Column(String, nullable=False, default="")

    user = relationship("User", back_populates="api_keys")

    @property
    def scope_set(self) -> frozenset[str]:
        """This key's scopes as a set (the column is comma-separated)."""
        return frozenset(s.strip() for s in (self.scopes or "").split(",") if s.strip())


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(String, nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=False, default="")
    status = Column(String, nullable=False, default=TicketStatus.open.value)
    priority = Column(String, nullable=False, default=TicketPriority.medium.value)
    archived = Column(Boolean, nullable=False, default=False)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    due_date = Column(DateTime, nullable=True)

    # code_blocks: list of {filename, line_start, line_end, content, language}
    code_blocks = Column(JSON, nullable=False, default=list)
    tags = Column(JSON, nullable=False, default=list)

    creator = relationship("User", foreign_keys=[created_by])
    assignee = relationship("User", foreign_keys=[assigned_to])
    comments = relationship(
        "Comment", back_populates="ticket", cascade="all, delete-orphan"
    )
    activities = relationship(
        "Activity", cascade="all, delete-orphan"
    )
    agent_runs = relationship(
        "AgentRun", cascade="all, delete-orphan"
    )
    # SQLite's FK enforcement is off (see database._sqlite_pragmas), so the
    # ondelete=CASCADE on TicketLease is documentation until it's turned on; this
    # relationship is what actually clears a lease when a ticket is hard-deleted.
    lease = relationship(
        "TicketLease", cascade="all, delete-orphan", uselist=False
    )


class TicketLease(Base):
    """An exclusive, time-limited claim on a ticket held by one worker.

    Claiming used to be implicit: the resolver took everything assigned to its
    bot id and stamped ``resolver:*`` tags on it, which is only safe because
    systemd runs exactly one sweep per bot. Opening the queue to third-party
    agents (or waking resolvers on events) makes two workers picking up the same
    ticket a live race, so the claim is made explicit here and the database — not
    a tag — is the arbiter.

    Two properties do the work:

    * ``ticket_id`` is **unique**, so "at most one holder" is enforced by the
      engine. Two concurrent claims cannot both win, however the SELECT that
      precedes them interleaves; the loser takes an ``IntegrityError`` and is
      answered 409.
    * ``expires_at`` bounds the claim. A worker that dies mid-ticket previously
      left ``resolver:planning`` on the ticket forever; now its lease simply
      lapses and the next sweep re-claims. Holders keep a long job alive by
      extending, which is a deliberate liveness/safety trade: a crashed worker
      stops extending, so the ticket returns to the queue after at most one TTL.

    ``token`` is an opaque secret handed to the claimant and required to release
    or extend. Without it any caller who can see the ticket could drop a rival's
    claim, which would make the lease decorative.

    The ``resolver:*`` tags remain as a human-readable mirror (see
    ``routers.tickets``), but this row is the source of truth.
    """
    __tablename__ = "ticket_leases"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(
        Integer, ForeignKey("tickets.id", ondelete="CASCADE"),
        unique=True, nullable=False, index=True,
    )
    worker_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token = Column(String, unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class AgentRun(Base):
    """One resolver phase (plan|implement|review) executed by an agent, with the
    token usage and cost it consumed. Lets the app surface the otherwise-invisible
    resolver work as an auditable, costed timeline per ticket.

    Mirrors the per-phase `token_usage` audit event the resolver writes to its
    JSONL log — this is the durable, app-visible copy of the same fact.
    """
    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False, index=True)
    agent = Column(String, nullable=False)        # claude | opencode | review-api | critique-api
    phase = Column(String, nullable=False)        # plan | implement | review | plan-critique
    model = Column(String, nullable=False, default="")
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cache_read_tokens = Column(Integer, nullable=False, default=0)
    cache_write_tokens = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=False, default=0.0)
    status = Column(String, nullable=False, default=AgentRunStatus.succeeded.value)
    # The tail of the agent's transcript, for a run that FAILED. Transcripts
    # otherwise live only on the machine the resolver runs on
    # (``resolver/logs/ticket-<id>-<phase>-<ts>.log``), which makes "why did
    # implement fail on #42?" unanswerable from the app — the one question the
    # runs table exists to help with. The resolver redacts it before sending, and
    # sends nothing at all for a run that succeeded: a successful transcript is
    # bulk with no reader.
    log_tail = Column(Text, nullable=False, default="")
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, default=utcnow, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False, index=True)
    author = Column(Integer, ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    ticket = relationship("Ticket", back_populates="comments")
    author_user = relationship("User", foreign_keys=[author])


class Notification(Base):
    """An in-app notification delivered to a single recipient.

    One row per (recipient, event). Strictly per-user — every endpoint filters by
    the authenticated user. Ticket/actor fields are *snapshots* (denormalized,
    mirroring how ``Activity.detail`` stores ``{name}``) so the inbox renders
    without joins and survives deletion of the ticket or actor; ``ticket_id`` is
    deliberately not an enforced FK for the same reason.

    ``type`` categorizes each notification (see :class:`NotificationType`) and is
    the seam for a future notification-settings panel: every notification carries
    a type and flows through the ``should_notify`` gate in ``inbox.py``.
    """
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)  # recipient
    type = Column(String, nullable=False)        # NotificationType value
    ticket_id = Column(Integer, nullable=True)   # not FK-enforced; snapshot below keeps it usable
    ticket_title = Column(String, nullable=False, default="")
    actor_id = Column(Integer, nullable=True)
    actor_name = Column(String, nullable=False, default="")
    comment_id = Column(Integer, nullable=True)
    read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class NotificationPreference(Base):
    """A single user's opt-out for one (type, channel) pair.

    Rows are sparse and default-on: the absence of a row means "enabled", so we
    only ever store explicit overrides. ``inbox.should_notify`` consults this
    table — a missing row, or ``enabled=True``, lets the notification through.
    The unique constraint keeps it to at most one row per (user, type, channel).
    """
    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "type", "channel", name="uq_notif_pref"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    type = Column(String, nullable=False)     # NotificationType value
    channel = Column(String, nullable=False)  # NotificationChannel value
    enabled = Column(Boolean, nullable=False, default=True)


class ResolverSettings(Base):
    """Server-managed, non-secret tunables for the resolver daemon.

    The resolver is normally configured from a ``.env`` file parsed once at
    process start. This table lets an admin override the *non-secret* tunables
    (model routing, attempt limits, verify gate, escalation, delegation) from the
    UI; the resolver fetches them at sweep start and overlays them on top of its
    ``.env`` defaults, so changes take effect on the next sweep.

    Keyed by ``bot_user_id`` to support multiple resolver identities (each with
    its own ``RESOLVER_ENV_FILE``); a ``NULL`` row is the global default used
    when a resolver has no row of its own. The whole tunable set is stored as a
    single JSON blob (mirroring ``Ticket.code_blocks``) so the resolver's config
    dataclass stays the schema of record. **Secrets never land here** — provider
    keys remain in ``.env`` and are surfaced read-only in the UI.
    """
    __tablename__ = "resolver_settings"
    __table_args__ = (
        UniqueConstraint("bot_user_id", name="uq_resolver_settings_bot"),
    )

    id = Column(Integer, primary_key=True, index=True)
    bot_user_id = Column(Integer, nullable=True)  # NULL = global default row
    settings = Column(JSON, nullable=False, default=dict)  # non-secret tunables only
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    updated_by = Column(Integer, nullable=True)


class SecuritySettings(Base):
    """Server-managed, admin-editable settings that affect the app's security
    posture (webhook SSRF exemptions, the insecure-webhooks/dispatcher-pause
    toggles, the lease TTL policy window, the per-user webhook cap).

    A single global row (id=1) — unlike :class:`ResolverSettings` there is no
    per-identity keying here, since these are app-wide policy, not per-bot
    tunables. Reading/writing this table is gated behind
    ``auth.require_recent_admin`` (admin role AND a session cookie minted
    within the last few minutes), not just ``auth.require_admin`` — these are
    exactly the settings an attacker holding a hijacked-but-valid admin
    session would most want to weaken quietly.
    """
    __tablename__ = "security_settings"

    id = Column(Integer, primary_key=True, index=True)
    settings = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    updated_by = Column(Integer, nullable=True)


class AgentInstance(Base):
    """A running agent's self-reported identity + observed state.

    Distinct from :class:`ResolverSettings` on purpose: settings are the
    *admin-authored overrides*, whereas this is what a worker *reports about
    itself* (which ``.env`` file it runs, its agent and model, and a snapshot of
    the non-secret config it's actually using). ``last_seen_at`` is bumped each
    heartbeat, so a stopped worker simply goes stale. **No secrets** —
    ``effective_config`` carries only non-secret tunables.

    Originally ``ResolverInstance``, one row per resolver bot. Nothing in the
    shape was resolver-specific, so it is now the registry for *any* worker: our
    own resolvers (``POST /resolvers/heartbeat``) and third-party agents
    authenticating with an ``agent``-scoped key (``POST /agents/heartbeat``).
    Both are read by the admin resolver-manager UI.
    """
    __tablename__ = "agent_instances"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_agent_instance_user"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)  # the worker's own user id
    label = Column(String, nullable=False, default="")   # RESOLVER_ENV_FILE, e.g. ".env.gemini"
    name = Column(String, nullable=False, default="")     # clean name, e.g. "gemini"
    agent = Column(String, nullable=False, default="")    # claude | opencode | ...
    model = Column(String, nullable=False, default="")
    effective_config = Column(JSON, nullable=False, default=dict)  # non-secret snapshot
    # Where this worker runs, and how often it promises to check in. Both are
    # reported rather than inferred: a station is a host, and several hosts can
    # run workers against one server. `heartbeat_seconds` is 0 for a worker that
    # only reports when it does a sweep, which is what tells a reader that a
    # long silence means "idle", not "dead" — the freshness rule needs the
    # cadence, and hardcoding one broke the moment sweep timers changed.
    station = Column(String, nullable=False, default="")
    heartbeat_seconds = Column(Integer, nullable=False, default=0)
    last_seen_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    # The column was `bot_user_id` while this was resolver-only. Kept as a synonym
    # (usable in queries and in the constructor) so resolver-side callers reading
    # "which bot is this" keep working against the widened name.
    bot_user_id = synonym("user_id")


# Back-compat alias for the pre-#56 name; the table and semantics are the same.
ResolverInstance = AgentInstance


class ChatRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"


class ChatConversation(Base):
    """One chat thread with the in-app assistant, owned by exactly one user.

    Strictly per-user, with **no admin override** — unlike tickets, where an admin
    may read anything. A thread embeds ticket content quoted at the time it was
    asked about, so letting an admin read someone's threads would be a second,
    weaker path to data the ticket ACL governs directly.

    ``ticket_id`` is the ticket the thread was opened from: an anchor for the
    context pack, not a hard scope. It is deliberately nullable — a thread started
    from the dashboard has no ticket — and re-resolved against the asker's own
    permissions on every turn, so it confers no access by itself.
    """
    __tablename__ = "chat_conversations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=True, index=True)
    # Derived from the first question rather than asked for, so a thread is
    # identifiable in the list without making the user name it.
    title = Column(String, nullable=False, default="")
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    messages = relationship(
        "ChatMessage", back_populates="conversation", cascade="all, delete-orphan"
    )


class ChatMessage(Base):
    """One turn in a thread. Assistant turns carry their own cost accounting.

    The accounting fields mirror :class:`AgentRun`'s on purpose: resolver work and
    chat work are both metered AI spend, and the app's rule is that AI spend is
    visible. Summing ``cost_usd`` over a user's turns since UTC midnight is also
    what enforces the daily cap.

    ``content`` stores what the *user* actually typed — never the assembled
    prompt. The context pack is rebuilt from live ticket data on every turn, so a
    stored thread can't serve stale ticket state back to the model, and can't
    become a durable copy of a ticket the user has since lost access to.

    ``meta`` is the open-ended per-turn extra (context ticket id, packed size,
    and later the tool calls and proposed actions), kept as a JSON blob — the same
    convention as ``Ticket.code_blocks`` — so a new field needs no migration.
    """
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(
        Integer, ForeignKey("chat_conversations.id"), nullable=False, index=True
    )
    role = Column(String, nullable=False)  # ChatRole value
    content = Column(Text, nullable=False, default="")
    model = Column(String, nullable=False, default="")
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=False, default=0.0)
    meta = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    conversation = relationship("ChatConversation", back_populates="messages")


class Activity(Base):
    """An immutable audit entry describing something that happened to a ticket.

    `action` is a short verb (e.g. "created", "status_changed"); `detail` is an
    optional JSON blob with the specifics (e.g. {"from": "open", "to": "resolved"}).
    """
    __tablename__ = "activities"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False, index=True)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String, nullable=False)
    detail = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    actor = relationship("User", foreign_keys=[actor_id])


class Outbox(Base):
    """One staged event of the transactional outbox (see ``events.emit``).

    Rows are written on the *caller's* session, so an event and the change it
    describes commit or roll back together — that is what buys at-least-once
    delivery without a message broker. Nothing reads this table yet; a
    dispatcher ships separately.

    `id` is monotonic and doubles as the sequence consumers order on. There are
    no foreign keys on `ticket_id`/`actor_id` (same choice as `Notification`) so
    a queued event survives deletion of the ticket or actor it names.

    `payload` is a *hint, not truth*: delivery retries reorder, so a consumer
    must re-fetch the ticket before acting on it.

    The dispatcher columns are NULL until a dispatcher touches the row.
    `claimed_at` is stamped when a batch is claimed and `delivered_at` when it
    lands, which is what lets the dispatcher claim / commit / send / commit —
    never holding SQLite's single write transaction open across network I/O.
    """
    __tablename__ = "outbox"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(String, nullable=False, index=True)
    ticket_id = Column(Integer, nullable=True, index=True)
    actor_id = Column(Integer, nullable=True)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    claimed_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)


class SavedView(Base):
    """A named, reusable ticket-list filter belonging to one user.

    `query` is the dashboard's raw URL query string (e.g.
    ``tag=repo:ticketing&status=open&sort=priority``) rather than a set of typed
    columns. The list page already keeps its whole filter state in the URL, so
    storing that string means a saved view and a shared link are the same thing —
    and a new filter can be added without a migration here.

    It is opaque to the backend: it is echoed back to the client, never parsed or
    executed server-side, so an unknown or malformed key can't do anything worse
    than produce a view that filters nothing.
    """
    __tablename__ = "saved_views"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_saved_view_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    query = Column(String, nullable=False, default="")
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Webhook(Base):
    """An HTTP endpoint subscribed to ticket events, owned by one user.

    ``user_id`` is the *owner*, and it is more than a label: deliveries are
    filtered against what that user can see (see ``routers.webhooks``), so a
    member's webhook can never carry a ticket they could not open in the UI.

    **On the secret.** This deliberately departs from :class:`ApiKey`, which
    stores only a sha256 hash. A signing secret has to be *recoverable* — the
    delivery worker computes an HMAC of each outbound body with it, and a hash
    cannot sign. So it is stored in plaintext and instead protected by never
    being readable: it is returned exactly once by create and once by rotate,
    and no read schema contains it (``WebhookOut`` has no ``secret`` field).
    ``secret_prefix`` is the non-secret label the UI shows in its place.
    """
    __tablename__ = "webhooks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False, default="")
    url = Column(String, nullable=False)
    # Empty list = every event type. Values are WebhookEventType strings.
    event_types = Column(JSON, nullable=False, default=list)
    # Empty list = no tag restriction; otherwise the ticket must carry ANY of
    # these tags (e.g. ["repo:foo"] to follow one repository).
    tag_filter = Column(JSON, nullable=False, default=list)
    secret = Column(String, nullable=False)        # plaintext; see the docstring
    secret_prefix = Column(String, nullable=False, default="")
    active = Column(Boolean, nullable=False, default=True)
    # Bumped by the delivery worker; a run of failures is what an operator needs
    # to see (and what an auto-disable policy would key on).
    consecutive_failures = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    deliveries = relationship("WebhookDelivery", cascade="all, delete-orphan")


class WebhookDelivery(Base):
    """One attempt-tracked delivery of one event to one webhook.

    This log is most of the feature's value: debugging somebody else's agent
    without it is guesswork, so a row is kept whether the send succeeded, failed
    or was never made.

    ``event_id`` points at the ``outbox`` row the delivery came from but is
    **not** an enforced foreign key — the outbox is prunable, and a pruned event
    must not cascade-delete its delivery history. ``event_type``, ``ticket_id``
    and ``payload`` are snapshots for the same reason (the same choice
    :class:`Notification` makes), and ``ticket_id`` is what the owner-visibility
    filter joins on.
    """
    __tablename__ = "webhook_deliveries"

    id = Column(Integer, primary_key=True, index=True)
    webhook_id = Column(Integer, ForeignKey("webhooks.id"), nullable=False, index=True)
    event_id = Column(Integer, nullable=True)     # outbox.id; not FK-enforced
    event_type = Column(String, nullable=False, default="")
    ticket_id = Column(Integer, nullable=True, index=True)  # snapshot, not FK
    payload = Column(JSON, nullable=True)         # what was (or will be) sent

    attempt_count = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=True)
    status_code = Column(Integer, nullable=True)
    response_snippet = Column(Text, nullable=False, default="")  # MAX_RESPONSE_SNIPPET
    error = Column(String, nullable=False, default="")
    state = Column(String, nullable=False, default=DeliveryState.pending.value)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
