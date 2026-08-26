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
from sqlalchemy.orm import relationship

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


class NotificationChannel(str, enum.Enum):
    """Where a notification is delivered. ``in_app`` is the bell/inbox;
    ``email`` is the SMTP path in ``notifications.py``."""
    in_app = "in_app"
    email = "email"


class AgentPhase(str, enum.Enum):
    plan = "plan"
    implement = "implement"
    review = "review"


class AgentRunStatus(str, enum.Enum):
    succeeded = "succeeded"
    failed = "failed"


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


class ResolverInstance(Base):
    """A running resolver's self-reported identity + observed state.

    Distinct from :class:`ResolverSettings` on purpose: settings are the
    *admin-authored overrides*, whereas this is what a resolver *reports about
    itself* at the start of each sweep (which ``.env`` file it runs, its agent
    and model, and a snapshot of the non-secret config it's actually using).
    Written by the resolver bot itself (``POST /resolvers/heartbeat``), read by
    the admin resolver-manager UI. ``last_seen_at`` is bumped each heartbeat, so
    a stopped resolver simply goes stale. **No secrets** — ``effective_config``
    carries only the same non-secret tunable set as ResolverSettings.
    """
    __tablename__ = "resolver_instances"
    __table_args__ = (
        UniqueConstraint("bot_user_id", name="uq_resolver_instance_bot"),
    )

    id = Column(Integer, primary_key=True, index=True)
    bot_user_id = Column(Integer, nullable=False, index=True)  # the resolver's own user id
    label = Column(String, nullable=False, default="")   # RESOLVER_ENV_FILE, e.g. ".env.gemini"
    name = Column(String, nullable=False, default="")     # clean name, e.g. "gemini"
    agent = Column(String, nullable=False, default="")    # claude | opencode | ...
    model = Column(String, nullable=False, default="")
    effective_config = Column(JSON, nullable=False, default=dict)  # non-secret snapshot
    last_seen_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


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
