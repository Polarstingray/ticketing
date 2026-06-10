"""SQLAlchemy ORM models: User, Ticket, Comment."""
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
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


# --- Models ------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    role = Column(String, nullable=False, default=UserRole.member.value)
    hashed_password = Column(String, nullable=False)
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

    user = relationship("User", back_populates="api_keys")


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
