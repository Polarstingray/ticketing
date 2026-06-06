"""Pydantic request/response schemas."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from models import TicketPriority, TicketStatus, TicketType, UserRole


# --- Users -------------------------------------------------------------------

class UserPublic(BaseModel):
    """User shape returned to other users (no secrets)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    email: str
    role: UserRole
    created_at: datetime


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
    created_at: datetime
    updated_at: datetime
    due_date: Optional[datetime]
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
    created_at: datetime


# --- Activity ----------------------------------------------------------------

class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    actor: Optional[int] = Field(default=None, validation_alias="actor_id")
    action: str
    detail: Optional[dict] = None
    created_at: datetime


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
    created_at: datetime
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked: bool


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1)
    expires_in_days: Optional[int] = Field(default=None, ge=1)


class ApiKeyCreated(ApiKeyMeta):
    """Returned exactly once on creation — includes the plaintext key."""
    api_key: str
