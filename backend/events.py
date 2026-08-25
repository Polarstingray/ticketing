"""Event bus: stage an ``Outbox`` row on the caller's session.

Same contract as ``activity.record_activity`` and ``inbox.create_notification``:
``emit`` only *stages* the row, and the calling route commits it as part of its
existing transaction. That is deliberate. Firing an HTTP call from a
``BackgroundTasks`` hook could announce a transaction that later rolled back,
and would lose the event outright if the process died first; writing the event
row inside the same transaction as the change it describes makes the two commit
or fail together, which is at-least-once delivery for free.

The payload is a **hint, not truth**. Delivery retries reorder, so by the time a
consumer sees an event the ticket may have moved on — consumers must re-fetch
the ticket before acting on it.

Event types:
  ``ticket.created``, ``ticket.assigned``, ``ticket.status_changed``,
  ``ticket.tagged``, ``comment.created``, ``agent_run.finished``
"""
from typing import Any, Optional

from sqlalchemy.orm import Session

from models import Outbox, Ticket, User


def emit(
    db: Session,
    *,
    type: str,
    ticket: Ticket,
    actor: Optional[User],
    delta: Optional[dict[str, Any]] = None,
) -> None:
    """Stage an outbox row for ``type`` on ``ticket``.

    ``actor`` is the user who caused the event, or None for a system-driven one.
    ``delta`` carries the before/after of a change event (e.g.
    ``{"from": old_status, "to": new_status}``); omit it for creation events.

    Call after ``db.flush()`` when the ticket is new, so ``ticket.id`` exists.
    """
    payload: dict[str, Any] = {
        "ticket_id": ticket.id,
        "ticket_title": ticket.title,
        "ticket_status": ticket.status,
        "ticket_priority": ticket.priority,
        "ticket_type": ticket.type,
        "ticket_tags": list(ticket.tags or []),
        "assigned_to": ticket.assigned_to,
        "actor_id": actor.id if actor else None,
        "actor_name": actor.display_name if actor else None,
    }
    if delta is not None:
        payload["delta"] = delta

    db.add(
        Outbox(
            type=type,
            ticket_id=ticket.id,
            actor_id=actor.id if actor else None,
            payload=payload,
        )
    )
