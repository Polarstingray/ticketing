"""SQLAlchemy ORM models: User, Ticket, Comment."""
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
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


# --- Models ------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    role = Column(String, nullable=False, default=UserRole.member.value)
    hashed_password = Column(String, nullable=False)
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
