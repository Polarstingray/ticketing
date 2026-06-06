"""Comment routes nested under a ticket."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from auth import can_view_ticket, get_current_user
from database import get_db
from models import Comment, Ticket, User
from schemas import CommentCreate, CommentOut

router = APIRouter(prefix="/tickets/{ticket_id}/comments", tags=["comments"])


def _ensure_ticket(ticket_id: int, db: Session) -> Ticket:
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


@router.get("", response_model=list[CommentOut])
def list_comments(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _ensure_ticket(ticket_id, db)
    # 404 (not 403) so non-members can't probe ticket existence.
    if not can_view_ticket(user, ticket):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return (
        db.query(Comment)
        .filter(Comment.ticket_id == ticket_id)
        .order_by(Comment.created_at.asc())
        .all()
    )


@router.post("", response_model=CommentOut, status_code=status.HTTP_201_CREATED)
def create_comment(
    ticket_id: int,
    payload: CommentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _ensure_ticket(ticket_id, db)
    # You shouldn't be able to comment on (or probe) a ticket you can't see.
    if not can_view_ticket(user, ticket):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    comment = Comment(ticket_id=ticket_id, author=user.id, body=payload.body)
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return comment
