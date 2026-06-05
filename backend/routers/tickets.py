"""Ticket routes: list/filter, create, retrieve, update, delete."""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from activity import record_activity
from auth import can_modify_ticket, get_current_user, is_admin
from database import get_db
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


@router.get("", response_model=PaginatedTickets)
def list_tickets(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
    status: Optional[TicketStatus] = Query(default=None),
    type: Optional[TicketType] = Query(default=None),
    assigned_to: Optional[int] = Query(default=None),
    created_by: Optional[int] = Query(default=None),
    priority: Optional[TicketPriority] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    q = db.query(Ticket)
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
        tags=payload.tags,
    )
    db.add(ticket)
    db.flush()  # assign ticket.id for the activity rows

    record_activity(db, ticket.id, user.id, "created")
    if assignee is not None:
        record_activity(
            db, ticket.id, user.id, "assigned",
            {"to": assignee.id, "name": assignee.display_name},
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
    _user: User = Depends(get_current_user),
):
    return _get_ticket_or_404(ticket_id, db)


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

    # Snapshot the fields we audit, before mutating.
    old_status = ticket.status
    old_priority = ticket.priority
    old_assigned_to = ticket.assigned_to

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
