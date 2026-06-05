"""Helper for recording ticket activity (the audit trail).

`record_activity` only stages the row on the session — the calling route commits
it as part of its existing transaction, so the activity entry and the change it
describes succeed or fail together.
"""
from typing import Optional

from sqlalchemy.orm import Session

from models import Activity


def record_activity(
    db: Session,
    ticket_id: int,
    actor_id: Optional[int],
    action: str,
    detail: Optional[dict] = None,
) -> None:
    db.add(
        Activity(
            ticket_id=ticket_id,
            actor_id=actor_id,
            action=action,
            detail=detail,
        )
    )
