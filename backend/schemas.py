"""Pydantic request/response schemas."""
from datetime import datetime, timezone
from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, PlainSerializer

from models import TicketPriority, TicketStatus, TicketType, UserRole

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


class TicketUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[TicketStatus] = None
    priority: Optional[TicketPriority] = None
    assigned_to: Optional[int] = None
    due_date: Optional[datetime] = None
    code_blocks: Optional[List[CodeBlock]] = None
    tags: Optional[List[str]] = None


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


# --- Agent runs --------------------------------------------------------------
# Allowed vocabularies are constrained via Literal so the resolver (or any caller)
# can't write garbage phase/agent/status values — bad input is rejected with 422.

AgentName = Literal["claude", "opencode"]
AgentPhaseName = Literal["plan", "implement", "review"]
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
