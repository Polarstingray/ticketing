"""Notification preference routes (the settings panel).

Scoped entirely to the authenticated user: a caller can only read and update
their own per-(type, channel) notification toggles. Preferences are sparse and
default-on — ``GET`` synthesizes the full matrix (every type x channel) with
``enabled=True`` wherever no explicit row exists, and ``PUT`` upserts the rows
the client sends, deleting any that are set back to the default (enabled) so the
table stays a record of explicit opt-outs only.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import (
    NotificationChannel,
    NotificationPreference,
    NotificationType,
    User,
)
from schemas import (
    NotificationPreferenceItem,
    NotificationPreferences,
    NotificationPreferencesUpdate,
)

router = APIRouter(prefix="/preferences", tags=["preferences"])


def _existing(db: Session, user_id: int) -> dict[tuple[str, str], NotificationPreference]:
    rows = (
        db.query(NotificationPreference)
        .filter(NotificationPreference.user_id == user_id)
        .all()
    )
    return {(r.type, r.channel): r for r in rows}


def _matrix(db: Session, user_id: int) -> NotificationPreferences:
    """The full type x channel grid, defaulting to enabled where no row exists."""
    rows = _existing(db, user_id)
    items = [
        NotificationPreferenceItem(
            type=t,
            channel=c,
            enabled=rows[(t.value, c.value)].enabled if (t.value, c.value) in rows else True,
        )
        for t in NotificationType
        for c in NotificationChannel
    ]
    return NotificationPreferences(items=items)


@router.get("", response_model=NotificationPreferences)
def get_preferences(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _matrix(db, user.id)


@router.put("", response_model=NotificationPreferences)
def update_preferences(
    payload: NotificationPreferencesUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = _existing(db, user.id)
    for item in payload.items:
        key = (item.type.value, item.channel.value)
        existing = rows.get(key)
        if item.enabled:
            # Default is on, so an enabled toggle needs no stored row — drop any
            # prior opt-out to keep the table to explicit overrides only.
            if existing is not None:
                db.delete(existing)
        elif existing is not None:
            existing.enabled = False
        else:
            db.add(
                NotificationPreference(
                    user_id=user.id,
                    type=item.type.value,
                    channel=item.channel.value,
                    enabled=False,
                )
            )
    db.commit()
    return _matrix(db, user.id)
