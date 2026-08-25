"""Server-Sent Events: tail the outbox as a live stream.

The outbox (``events.emit``) is written inside the transaction that changes a
ticket; this endpoint is the first consumer of it. It exists so a resolver can
learn about an assignment in about a second instead of waiting out its poll
timer, *without* anyone opening a port to the resolver — the client holds an
outbound connection, so a dev station behind NAT works with no tunnel.

**Visibility is the same rule as ``tickets._visible_tickets``**: a non-admin
sees only events on tickets they created or are assigned to. It is applied by
joining the outbox row back to its ticket rather than trusting the event
payload, because the payload is a snapshot from emit time — a ticket reassigned
away after the event was staged must stop being visible, and the join is what
makes that true. An event whose ticket has since been deleted is dropped for
non-admins for the same reason: there is nothing left to authorize against.

Two design notes that matter more than they look:

*Sessions are per-poll, never per-connection.* A stream lives for hours; the
database is SQLite, which has a single writer. Holding one request-scoped
session open for the life of the connection would pin a read snapshot (so the
stream would never see new rows) and stand in the way of writers. Each tick
therefore opens a session, reads, and closes it.

*A fresh connection starts at the current head*, not at outbox row 1. Replaying
all history to every client that connects would turn a reconnect storm into a
stampede. A client that needs the gap covered says so explicitly with
``last_event_id`` (or the standard ``Last-Event-ID`` header that EventSource
resends on reconnect); a client that misses events entirely still has its poll
timer as the safety net, which is the intended degradation.
"""
import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from auth import get_current_user, is_admin
from database import SessionLocal
from models import Outbox, Ticket, User

router = APIRouter(prefix="/events", tags=["events"])

# How often the tail is polled for new rows. The outbox is a local table and the
# query is an indexed `id >` range scan, so a sub-second cadence is cheap; this
# is the dominant term in end-to-end wakeup latency.
POLL_INTERVAL = 0.5

# Idle gap after which a comment line is sent. Proxies and load balancers hang up
# on a connection that has been silent for too long (nginx's default read timeout
# is 60s), and the write is also how a server-side generator notices the client
# is gone when `is_disconnected` doesn't fire.
HEARTBEAT_INTERVAL = 15.0

# Ceiling on rows handed out per tick, so a client resuming from a far-behind
# `last_event_id` streams the backlog in batches instead of building one huge
# response body. The next tick continues where this one stopped.
BATCH_LIMIT = 100


def visible_events(
    db: Session, user_id: int, admin: bool, after_id: int, limit: int = BATCH_LIMIT
) -> list[Outbox]:
    """Outbox rows after ``after_id`` that ``user_id`` is entitled to read.

    Mirrors ``tickets._visible_tickets``. Admins see the whole outbox; everyone
    else gets the inner join to `Ticket`, which both applies the created/assigned
    rule and drops events whose ticket no longer exists.
    """
    query = db.query(Outbox).filter(Outbox.id > after_id)
    if not admin:
        query = query.join(Ticket, Ticket.id == Outbox.ticket_id).filter(
            or_(Ticket.created_by == user_id, Ticket.assigned_to == user_id)
        )
    return query.order_by(Outbox.id.asc()).limit(limit).all()


def current_head(db: Session) -> int:
    """Highest outbox id, or 0 when the table is empty."""
    return db.query(Outbox.id).order_by(Outbox.id.desc()).limit(1).scalar() or 0


def format_event(row: Outbox) -> str:
    """Render one outbox row as an SSE frame.

    `id:` is the outbox id, which is what a client echoes back as
    ``last_event_id`` to resume. The payload is passed through as emitted — it is
    a hint, and a consumer is expected to re-fetch the ticket before acting.
    """
    payload: dict[str, Any] = dict(row.payload or {})
    payload.setdefault("ticket_id", row.ticket_id)
    payload["event_id"] = row.id
    payload["type"] = row.type
    data = json.dumps(payload, default=str)
    return f"id: {row.id}\nevent: {row.type}\ndata: {data}\n\n"


async def event_stream(request: Request, user_id: int, admin: bool, after_id: int):
    """Yield SSE frames for ``user_id`` until the client goes away.

    ``request`` is only consulted through ``is_disconnected()``, so tests can
    drive this with any object exposing that coroutine.
    """
    last_id = after_id
    # Tell the client where it is starting, so a resume after this connection
    # drops has a cursor even if no event ever arrives on it.
    yield f": connected at {last_id}\n\n"
    idle = 0.0
    while True:
        if await request.is_disconnected():
            return

        db = SessionLocal()
        try:
            rows = visible_events(db, user_id, admin, last_id)
            # Advance past everything committed so far, not just the rows this
            # caller may read — otherwise an event for someone else is re-scanned
            # on every tick forever, and the scan grows without bound.
            head = current_head(db)
            frames = [format_event(row) for row in rows]
            next_id = rows[-1].id if len(rows) == BATCH_LIMIT else max(head, last_id)
        finally:
            db.close()

        last_id = next_id
        if frames:
            idle = 0.0
            for frame in frames:
                yield frame
        else:
            idle += POLL_INTERVAL
            if idle >= HEARTBEAT_INTERVAL:
                idle = 0.0
                yield ": keepalive\n\n"

        await asyncio.sleep(POLL_INTERVAL)


@router.get("/stream")
async def stream(
    request: Request,
    last_event_id: Optional[int] = Query(
        None,
        ge=0,
        description="Resume after this outbox id. Omitted means start at the current head.",
    ),
    user: User = Depends(get_current_user),
):
    """Tail the outbox as `text/event-stream`, scoped to the caller."""
    # Read the identity out of the request-scoped session now: that session is
    # released when this handler returns, long before the generator finishes, so
    # `user` must not be touched from inside the stream.
    user_id = user.id
    admin = is_admin(user)

    if last_event_id is None:
        header = request.headers.get("Last-Event-ID", "").strip()
        if header.isdigit():
            last_event_id = int(header)
    if last_event_id is None:
        db = SessionLocal()
        try:
            last_event_id = current_head(db)
        finally:
            db.close()

    return StreamingResponse(
        event_stream(request, user_id, admin, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx buffers proxied responses by default, which would hold each
            # frame until the buffer fills and defeat the whole point.
            "X-Accel-Buffering": "no",
        },
    )
