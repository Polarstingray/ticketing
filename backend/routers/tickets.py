"""Ticket routes: list/filter, create, retrieve, update, delete."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from auth import can_modify_ticket, get_current_user, is_admin
from database import get_db
from models import Ticket, TicketPriority, TicketStatus, TicketType, User, utcnow
from schemas import TicketCreate, TicketOut, TicketUpdate

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _get_ticket_or_404(ticket_id: int, db: Session) -> Ticket:
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


@router.get("", response_model=list[TicketOut])
def list_tickets(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
    status: Optional[TicketStatus] = Query(default=None),
    type: Optional[TicketType] = Query(default=None),
    assigned_to: Optional[int] = Query(default=None),
    created_by: Optional[int] = Query(default=None),
    priority: Optional[TicketPriority] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    archived: Optional[bool] = Query(default=None),
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
    # Archived tickets are hidden by default; pass archived=true for the archive view.
    if archived is None:
        q = q.filter(Ticket.archived == False)  # noqa: E712
    else:
        q = q.filter(Ticket.archived == archived)
    tickets = q.order_by(Ticket.created_at.desc()).all()
    if tag is not None:
        tickets = [t for t in tickets if tag in (t.tags or [])]
    return tickets


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if payload.assigned_to is not None:
        if not db.query(User).filter(User.id == payload.assigned_to).first():
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
    db.commit()
    db.refresh(ticket)
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
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(ticket_id, db)
    if not can_modify_ticket(user, ticket):
        raise HTTPException(status_code=403, detail="Not permitted to modify this ticket")

    data = payload.model_dump(exclude_unset=True)

    if "assigned_to" in data and data["assigned_to"] is not None:
        if not db.query(User).filter(User.id == data["assigned_to"]).first():
            raise HTTPException(status_code=400, detail="assigned_to user does not exist")

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

    ticket.updated_at = utcnow()
    db.commit()
    db.refresh(ticket)
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
