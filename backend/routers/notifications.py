"""In-app notification routes (the bell/inbox).

Every endpoint is scoped to the authenticated user: a notification belonging to
another user is invisible (404 on single-row access, silently skipped in bulk
ops), matching the IDOR convention used in ``comments.py`` / ``tickets.py``.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import Notification, User
from schemas import (
    BulkDeleteRequest,
    NotificationList,
    NotificationOut,
    UnreadCount,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _unread_count(db: Session, user_id: int) -> int:
    return (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.read == False)  # noqa: E712
        .count()
    )


def _get_own_or_404(notification_id: int, db: Session, user: User) -> Notification:
    n = (
        db.query(Notification)
        .filter(Notification.id == notification_id, Notification.user_id == user.id)
        .first()
    )
    if not n:
        # 404 (not 403) so a user can't probe which notification ids exist.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    return n


@router.get("", response_model=NotificationList)
def list_notifications(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    unread: Optional[bool] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    q = db.query(Notification).filter(Notification.user_id == user.id)
    if unread is not None:
        # unread=true -> only unread (read == False); unread=false -> only read.
        q = q.filter(Notification.read == (not unread))  # noqa: E712
    total = q.count()
    items = (
        q.order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return NotificationList(
        items=items,
        total=total,
        unread_count=_unread_count(db, user.id),
        limit=limit,
        offset=offset,
    )


@router.get("/unread_count", response_model=UnreadCount)
def unread_count(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return UnreadCount(unread_count=_unread_count(db, user.id))


@router.post("/read_all", response_model=UnreadCount)
def mark_all_read(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.read == False)  # noqa: E712
        .update({Notification.read: True}, synchronize_session=False)
    )
    db.commit()
    return UnreadCount(unread_count=0)


@router.post("/read_by_ticket/{ticket_id}", response_model=UnreadCount)
def mark_read_by_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Clear the caller's unread notifications for one ticket.

    Called when a ticket detail page is opened, so the list-view dot for that
    ticket goes away without the client having to enumerate notification ids.
    Scoped to the caller like every other route here; an unknown ticket id is
    simply a no-op rather than a 404, since ``ticket_id`` is a denormalized
    snapshot (not an FK) and may point at a since-deleted ticket.
    """
    (
        db.query(Notification)
        .filter(
            Notification.user_id == user.id,
            Notification.ticket_id == ticket_id,
            Notification.read == False,  # noqa: E712
        )
        .update({Notification.read: True}, synchronize_session=False)
    )
    db.commit()
    return UnreadCount(unread_count=_unread_count(db, user.id))


@router.post("/bulk_delete")
def bulk_delete(
    payload: BulkDeleteRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(Notification).filter(Notification.user_id == user.id)
    if payload.all:
        deleted = q.delete(synchronize_session=False)
    elif payload.ids:
        # Only ever touches the caller's rows; ids they don't own are ignored.
        deleted = q.filter(Notification.id.in_(payload.ids)).delete(synchronize_session=False)
    else:
        deleted = 0
    db.commit()
    return {"deleted": deleted}


@router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_read(
    notification_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    n = _get_own_or_404(notification_id, db, user)
    if not n.read:
        n.read = True
        db.commit()
        db.refresh(n)
    return n


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    n = _get_own_or_404(notification_id, db, user)
    db.delete(n)
    db.commit()
    return None
