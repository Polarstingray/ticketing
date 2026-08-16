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
