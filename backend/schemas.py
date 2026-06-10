"""Pydantic request/response schemas."""
from datetime import datetime, timezone
import re
from typing import Annotated, List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, PlainSerializer, field_validator

from models import TicketPriority, TicketStatus, TicketType, UserRole

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


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1)
    expires_in_days: Optional[int] = Field(default=None, ge=1)


class ApiKeyCreated(ApiKeyMeta):
    """Returned exactly once on creation — includes the plaintext key."""
    api_key: str
