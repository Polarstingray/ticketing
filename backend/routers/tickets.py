"""Ticket routes: list/filter, create, retrieve, update, delete."""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from activity import record_activity
from auth import can_modify_ticket, can_view_ticket, get_current_user, is_admin
from control_tags import can_manage_reserved_tags, is_reserved_tag, reserved_subset
from database import get_db
from inbox import create_notification
from models import (
    Activity,
    Ticket,
    TicketPriority,
    TicketStatus,
    TicketType,
    User,
    utcnow,
)
from notifications import notify_assignment, notify_new_ticket_admins
from schemas import (
    ActivityOut,
    PaginatedTickets,
    TicketCreate,
    TicketOut,
    TicketUpdate,
)

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _get_ticket_or_404(ticket_id: int, db: Session) -> Ticket:
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


_RESERVED_TAG_ERROR = (
    "Reserved tags (claude:*, repo:*, dangerous, fix) cannot be set; "
    "they are managed by the automation."
)


def _resolve_tags(submitted: list[str], existing: list[str] | None) -> list[str]:
    """Compute the tag set to store for a non-trusted caller's request.

    Trusted callers (admin / resolver bot) bypass this and get a full replace.
    For everyone else the ``tags`` field controls only *free* (non-reserved)
    tags: submitting any reserved tag is rejected, and the ticket's existing
    reserved tags are preserved so a free-tag edit can never add, remove, or
    alter a control tag.
    """
    if reserved_subset(submitted):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=_RESERVED_TAG_ERROR)
    existing_reserved = [t for t in (existing or []) if is_reserved_tag(t)]
    # submitted contains only free tags here (reserved rejected above).
    return existing_reserved + [t for t in submitted if t not in existing_reserved]


@router.get("", response_model=PaginatedTickets)
def list_tickets(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    status: Optional[TicketStatus] = Query(default=None),
    type: Optional[TicketType] = Query(default=None),
    assigned_to: Optional[int] = Query(default=None),
    created_by: Optional[int] = Query(default=None),
    priority: Optional[TicketPriority] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    archived: Optional[bool] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    q = db.query(Ticket)
    # Non-admins may only see tickets they created or are assigned to;
    # code_review tickets embed private source in code_blocks.
    if not is_admin(user):
        q = q.filter(or_(Ticket.created_by == user.id, Ticket.assigned_to == user.id))
    if status is not None:
        q = q.filter(Ticket.status == status.value)
    if type is not None:
        q = q.filter(Ticket.type == type.value)
    if assigned_to is not None:
        q = q.filter(Ticket.assigned_to == assigned_to)
    if created_by is not None:
        q = q.filter(Ticket.created_by == created_by)
    if priority is not None:
        q = q.filter(Ticket.priority == priority.value)
    # Archived tickets are hidden by default; pass archived=true for the archive view.
    if archived is None:
        q = q.filter(Ticket.archived == False)  # noqa: E712
    else:
        q = q.filter(Ticket.archived == archived)
    if tag is not None:
        # tags are stored as a JSON text array (e.g. '["auth", "urgent"]'); match the
        # quoted token in SQL so the filter composes with LIMIT/OFFSET. This is a
        # substring match, so a tag that is a substring of another could over-match —
        # acceptable for our exact-token usage.
        q = q.filter(Ticket.tags.like(f'%"{tag}"%'))

    total = q.count()
    items = (
        q.order_by(Ticket.created_at.desc()).offset(offset).limit(limit).all()
    )
    return PaginatedTickets(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assignee = None
    if payload.assigned_to is not None:
        assignee = db.query(User).filter(User.id == payload.assigned_to).first()
        if not assignee:
            raise HTTPException(status_code=400, detail="assigned_to user does not exist")

    # code_blocks only carry meaning for code_review tickets.
    code_blocks = (
        [cb.model_dump() for cb in payload.code_blocks]
        if payload.type == TicketType.code_review
        else []
    )

    # Only trusted automation (admin / resolver bot) may set reserved control
    # tags; everyone else may set free tags only. (No existing tags on create.)
    tags = payload.tags
    if tags and not can_manage_reserved_tags(user):
        tags = _resolve_tags(tags, existing=None)

    ticket = Ticket(
        type=payload.type.value,
        title=payload.title,
        description=payload.description,
        status=payload.status.value,
        priority=payload.priority.value,
        created_by=user.id,
        assigned_to=payload.assigned_to,
        due_date=payload.due_date,
        code_blocks=code_blocks,
        tags=tags,
    )
    db.add(ticket)
    db.flush()  # assign ticket.id for the activity rows

    record_activity(db, ticket.id, user.id, "created")
    if assignee is not None:
        record_activity(
            db, ticket.id, user.id, "assigned",
            {"to": assignee.id, "name": assignee.display_name},
        )
        create_notification(
            db, user_id=assignee.id, type="assigned", ticket=ticket, actor=user,
        )
    db.commit()
    db.refresh(ticket)

    # Notify admins of the new ticket, and the assignee (if any, and not the author).
    notify_new_ticket_admins(background, db, ticket, user)
    if assignee is not None:
        notify_assignment(background, ticket, assignee, user)
    return ticket


@router.get("/{ticket_id}", response_model=TicketOut)
def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(ticket_id, db)
    # Return 404 (not 403) so non-members can't confirm a ticket ID exists.
    if not can_view_ticket(user, ticket):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


@router.patch("/{ticket_id}", response_model=TicketOut)
def update_ticket(
    ticket_id: int,
    payload: TicketUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(ticket_id, db)
    if not can_modify_ticket(user, ticket):
        raise HTTPException(status_code=403, detail="Not permitted to modify this ticket")

    data = payload.model_dump(exclude_unset=True)

    new_assignee = None
    if "assigned_to" in data and data["assigned_to"] is not None:
        new_assignee = db.query(User).filter(User.id == data["assigned_to"]).first()
        if not new_assignee:
            raise HTTPException(status_code=400, detail="assigned_to user does not exist")

    # Reserved control tags drive the resolver's automation, so only trusted
    # identities (admin / resolver bot) may set them. For everyone else, a tags
    # edit controls free tags only and the ticket's existing reserved tags are
    # preserved — see _resolve_tags.
    if "tags" in data and data["tags"] is not None and not can_manage_reserved_tags(user):
        data["tags"] = _resolve_tags(data["tags"], existing=ticket.tags)

    # Snapshot the fields we audit, before mutating.
    old_status = ticket.status
    old_priority = ticket.priority
    old_assigned_to = ticket.assigned_to
    old_tags = list(ticket.tags or [])

    for field in ("title", "description", "status", "priority", "assigned_to", "due_date", "tags"):
        if field in data:
            value = data[field]
            # Enum fields arrive as Enum instances; store their value.
            if field in ("status", "priority") and value is not None:
                value = value.value if hasattr(value, "value") else value
            setattr(ticket, field, value)

    if "code_blocks" in data and data["code_blocks"] is not None:
        ticket.code_blocks = [
            cb if isinstance(cb, dict) else cb.model_dump() for cb in data["code_blocks"]
        ]

    # Record activity for the audited fields that actually changed.
    if "status" in data and ticket.status != old_status:
        record_activity(db, ticket.id, user.id, "status_changed",
                        {"from": old_status, "to": ticket.status})
    if "priority" in data and ticket.priority != old_priority:
        record_activity(db, ticket.id, user.id, "priority_changed",
                        {"from": old_priority, "to": ticket.priority})
    if "assigned_to" in data and ticket.assigned_to != old_assigned_to:
        if ticket.assigned_to is None:
            record_activity(db, ticket.id, user.id, "unassigned")
        else:
            record_activity(db, ticket.id, user.id, "assigned",
                            {"to": new_assignee.id, "name": new_assignee.display_name})
            create_notification(
                db, user_id=new_assignee.id, type="assigned", ticket=ticket, actor=user,
            )
    if "tags" in data and data["tags"] is not None:
        new_tags = list(ticket.tags or [])
        if new_tags != old_tags:
            added = [t for t in new_tags if t not in old_tags]
            removed = [t for t in old_tags if t not in new_tags]
            record_activity(db, ticket.id, user.id, "tags_changed",
                            {"added": added, "removed": removed})

    ticket.updated_at = utcnow()
    db.commit()
    db.refresh(ticket)

    # Notify a newly-assigned (non-self) user.
    if new_assignee is not None and old_assigned_to != new_assignee.id:
        notify_assignment(background, ticket, new_assignee, user)
    return ticket


@router.delete("/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(ticket_id, db)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    db.delete(ticket)
    db.commit()
    return None


@router.get("/{ticket_id}/activity", response_model=list[ActivityOut])
def list_activity(
    ticket_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    _get_ticket_or_404(ticket_id, db)
    return (
        db.query(Activity)
        .filter(Activity.ticket_id == ticket_id)
        .order_by(Activity.created_at.asc(), Activity.id.asc())
        .all()
    )


@router.post("/{ticket_id}/archive", response_model=TicketOut)
def archive_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(ticket_id, db)
    if not can_modify_ticket(user, ticket):
        raise HTTPException(status_code=403, detail="Not permitted to modify this ticket")
    if ticket.status != TicketStatus.closed.value:
        raise HTTPException(status_code=400, detail="Only closed tickets can be archived")
    ticket.archived = True
    ticket.updated_at = utcnow()
    db.commit()
    db.refresh(ticket)
    return ticket


@router.post("/{ticket_id}/unarchive", response_model=TicketOut)
def unarchive_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(ticket_id, db)
    if not can_modify_ticket(user, ticket):
        raise HTTPException(status_code=403, detail="Not permitted to modify this ticket")
    ticket.archived = False
    ticket.updated_at = utcnow()
    db.commit()
    db.refresh(ticket)
    return ticket
