"""In-app notification creation (the bell/inbox), kept separate from the
email-focused ``notifications.py``.

Like ``activity.record_activity``, the helpers here only *stage* rows on the
session — the calling route commits them as part of its existing transaction, so
the notification and the change it describes succeed or fail together.

Each notification snapshots the ticket title and actor name onto the row, so the
inbox renders standalone and the entry survives deletion of the ticket or actor.

``should_notify`` is the seam for a future notification-settings panel: every
notification flows through it. It currently always returns ``True``; once a
``NotificationPreference`` table exists it can consult per-user/type/channel
preferences (default-on when no row exists) without touching any event site.
"""
from typing import Optional

from sqlalchemy.orm import Session

from models import Notification, NotificationPreference, Ticket, User


def should_notify(
    db: Session,
    user_id: int,
    type: str,
    channel: str = "in_app",
) -> bool:
    """Gate for whether a recipient should receive a notification of ``type`` on
    ``channel``.

    Consults the ``NotificationPreference`` table keyed by (user_id, type,
    channel). Preferences are sparse and default-on: a missing row means enabled,
    so only explicit opt-outs ever suppress a notification.
    """
    pref = (
        db.query(NotificationPreference)
        .filter(
            NotificationPreference.user_id == user_id,
            NotificationPreference.type == type,
            NotificationPreference.channel == channel,
        )
        .first()
    )
    if pref is None:
        return True
    return pref.enabled


def create_notification(
    db: Session,
    *,
    user_id: Optional[int],
    type: str,
    ticket: Ticket,
    actor: Optional[User],
    comment_id: Optional[int] = None,
) -> None:
    """Stage a notification for ``user_id`` about ``ticket``.

    No-op when there's no recipient, when the recipient is the actor (you never
    notify yourself), or when ``should_notify`` declines.
    """
    if user_id is None:
        return
    if actor is not None and user_id == actor.id:
        return
    if not should_notify(db, user_id, type):
        return

    db.add(
        Notification(
            user_id=user_id,
            type=type,
            ticket_id=ticket.id,
            ticket_title=ticket.title or "",
            actor_id=actor.id if actor else None,
            actor_name=(actor.display_name if actor else "") or "",
            comment_id=comment_id,
        )
    )


def notify_comment_recipients(
    db: Session,
    ticket: Ticket,
    comment,
    actor: User,
) -> None:
    """Notify everyone involved in ``ticket`` (assignee + creator) that ``actor``
    commented, except the commenter themselves."""
    recipients = {ticket.assigned_to, ticket.created_by}
    recipients.discard(None)
    recipients.discard(actor.id if actor else None)
    for user_id in recipients:
        create_notification(
            db,
            user_id=user_id,
            type="commented",
            ticket=ticket,
            actor=actor,
            comment_id=comment.id,
        )
