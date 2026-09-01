"""Comment routes nested under a ticket."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from activity import record_activity
from auth import can_view_ticket, get_current_user, is_admin
from database import get_db
from events import emit
from inbox import notify_comment_recipients
from models import Comment, Ticket, User
from notifications import notify_comment_email
from schemas import CommentCreate, CommentOut, CommentUpdate, PaginatedComments

router = APIRouter(prefix="/tickets/{ticket_id}/comments", tags=["comments"])


def _ensure_ticket(ticket_id: int, db: Session) -> Ticket:
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


def _ensure_comment(comment_id: int, db: Session) -> Comment:
    comment = db.query(Comment).filter(Comment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
    return comment


@router.get("", response_model=PaginatedComments)
def list_comments(
    ticket_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _ensure_ticket(ticket_id, db)
    # 404 (not 403) so non-members can't probe ticket existence.
    if not can_view_ticket(user, ticket):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    q = (
        db.query(Comment)
        .filter(Comment.ticket_id == ticket_id)
        .order_by(Comment.created_at.asc())
    )
    total = q.count()
    items = q.offset(offset).limit(limit).all()
    return PaginatedComments(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=CommentOut, status_code=status.HTTP_201_CREATED)
def create_comment(
    ticket_id: int,
    payload: CommentCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _ensure_ticket(ticket_id, db)
    # You shouldn't be able to comment on (or probe) a ticket you can't see.
    if not can_view_ticket(user, ticket):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    comment = Comment(ticket_id=ticket_id, author=user.id, body=payload.body)
    db.add(comment)
    db.flush()  # assign comment.id
    record_activity(db, ticket_id, user.id, "commented", {"comment_id": comment.id})
    notify_comment_recipients(db, ticket, comment, user)
    emit(db, type="comment.created", ticket=ticket, actor=user,
         delta={"comment_id": comment.id})
    notify_comment_email(background, db, ticket, comment, user)
    db.commit()
    db.refresh(comment)
    return comment


@router.patch(
    "/{comment_id}", response_model=CommentOut, status_code=status.HTTP_200_OK
)
def update_comment(
    ticket_id: int,
    comment_id: int,
    payload: CommentUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _ensure_ticket(ticket_id, db)
    if not can_view_ticket(user, ticket):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    comment = _ensure_comment(comment_id, db)
    if comment.ticket_id != ticket_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found in ticket")

    if comment.author != user.id and not is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to edit this comment")

    comment.body = payload.body
    db.add(comment)
    db.commit()
    db.refresh(comment)
    record_activity(db, ticket_id, user.id, "edited comment", {"comment_id": comment.id})
    return comment


@router.delete(
    "/{comment_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_comment(
    ticket_id: int,
    comment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _ensure_ticket(ticket_id, db)
    if not can_view_ticket(user, ticket):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    comment = _ensure_comment(comment_id, db)
    if comment.ticket_id != ticket_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found in ticket")

    if comment.author != user.id and not is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to delete this comment")

    db.delete(comment)
    db.commit()
    record_activity(db, ticket_id, user.id, "deleted comment", {"comment_id": comment.id})
    return
