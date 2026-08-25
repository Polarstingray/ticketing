"""Tests for the SSE outbox tail (``routers/events.py``).

The endpoint's body is an endless generator, and `TestClient` cannot read one:
it drives the app through a blocking portal where `is_disconnected()` never
fires, so iterating the body and closing it leaves the generator looping and the
test hanging. Hence the split. Auth and request validation go through the client,
because those reject before the body is ever entered. Everything downstream of
that — visibility, the resume cursor, the response headers — is driven directly
against the handler and its generator, with a stub request that hangs up on cue.
That stub is also the only way to assert the stream *stops*, which is the
property that keeps a dropped client from leaking a poller forever.
"""
import asyncio
import json

import pytest
from starlette.datastructures import Headers

from database import SessionLocal
from events import emit
from models import Outbox, Ticket, User
from routers.events import (
    current_head,
    event_stream,
    format_event,
    stream,
    visible_events,
)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _make_ticket(db, creator_id: int, assigned_to: int | None = None) -> Ticket:
    ticket = Ticket(
        type="task", title="stream fixture", description="d",
        status="open", priority="medium",
        created_by=creator_id, assigned_to=assigned_to,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def _emit(db, ticket, actor_id, type="ticket.assigned"):
    actor = db.query(User).filter(User.id == actor_id).first()
    emit(db, type=type, ticket=ticket, actor=actor)
    db.commit()


class StubRequest:
    """Stands in for ``Request``: disconnects after ``after`` checks.

    ``after=0`` hangs up immediately, which is how the "stops when the client
    goes away" case is expressed.
    """

    def __init__(self, after: int = 1):
        self.after = after
        self.checks = 0

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks > self.after


def _drain(request, user_id, admin, after_id) -> list[str]:
    """Run the generator to completion and collect every frame it yielded."""
    async def run():
        return [frame async for frame in event_stream(request, user_id, admin, after_id)]
    return asyncio.run(run())


# --- the query seam ---------------------------------------------------------

def test_visible_events_applies_the_ticket_read_boundary(db, admin_id, make_user):
    """A member sees events on their own tickets and nobody else's."""
    mine = make_user()
    theirs = make_user()
    head = current_head(db)

    my_ticket = _make_ticket(db, admin_id, assigned_to=mine.id)
    their_ticket = _make_ticket(db, admin_id, assigned_to=theirs.id)
    _emit(db, my_ticket, admin_id)
    _emit(db, their_ticket, admin_id)

    ids = [r.ticket_id for r in visible_events(db, mine.id, False, head)]
    assert my_ticket.id in ids
    assert their_ticket.id not in ids


def test_visible_events_includes_tickets_i_created(db, make_user):
    """`_visible_tickets` is created-by OR assigned-to; the stream matches it."""
    author = make_user()
    head = current_head(db)
    ticket = _make_ticket(db, author.id)
    _emit(db, ticket, author.id, type="ticket.created")

    assert ticket.id in [r.ticket_id for r in visible_events(db, author.id, False, head)]


def test_visible_events_admin_sees_everything(db, admin_id, make_user):
    member = make_user()
    head = current_head(db)
    ticket = _make_ticket(db, member.id, assigned_to=member.id)
    _emit(db, ticket, member.id)

    assert ticket.id in [r.ticket_id for r in visible_events(db, admin_id, True, head)]


def test_visible_events_resumes_after_the_cursor(db, admin_id):
    """`after_id` is exclusive, so a resumed client never re-reads a frame."""
    ticket = _make_ticket(db, admin_id, assigned_to=admin_id)
    _emit(db, ticket, admin_id)
    first = visible_events(db, admin_id, True, current_head(db) - 1)[-1]

    _emit(db, ticket, admin_id)
    resumed = visible_events(db, admin_id, True, first.id)

    assert first.id not in [r.id for r in resumed]
    assert all(r.id > first.id for r in resumed)


def test_visible_events_hides_orphaned_events_from_members(db, admin_id, make_user):
    """An event whose ticket is gone has nothing left to authorize against."""
    member = make_user()
    head = current_head(db)
    db.add(Outbox(type="ticket.assigned", ticket_id=None, actor_id=admin_id, payload={}))
    db.commit()

    assert visible_events(db, member.id, False, head) == []


# --- frame rendering --------------------------------------------------------

def test_format_event_renders_an_sse_frame(db, admin_id):
    ticket = _make_ticket(db, admin_id, assigned_to=admin_id)
    _emit(db, ticket, admin_id)
    row = db.query(Outbox).order_by(Outbox.id.desc()).first()

    frame = format_event(row)

    assert frame.startswith(f"id: {row.id}\n")
    assert "event: ticket.assigned\n" in frame
    assert frame.endswith("\n\n")
    data = frame.split("data: ", 1)[1].strip()
    payload = json.loads(data)
    assert payload["ticket_id"] == ticket.id
    assert payload["event_id"] == row.id
    assert payload["type"] == "ticket.assigned"


# --- the generator ----------------------------------------------------------

def test_stream_opens_with_the_cursor(db, admin_id):
    frames = _drain(StubRequest(after=0), admin_id, True, 41)
    assert frames == [": connected at 41\n\n"]


def test_stream_delivers_an_assignment_event(db, admin_id, make_user):
    """The end-to-end path: PATCH assigns, emit stages, the stream ships it."""
    member = make_user()
    head = current_head(db)
    ticket = _make_ticket(db, admin_id, assigned_to=member.id)
    _emit(db, ticket, admin_id)

    frames = _drain(StubRequest(after=1), member.id, False, head)

    body = "".join(frames)
    assert "event: ticket.assigned" in body
    assert f'"ticket_id": {ticket.id}' in body


def test_stream_does_not_leak_another_users_event(db, admin_id, make_user):
    mine = make_user()
    theirs = make_user()
    head = current_head(db)
    ticket = _make_ticket(db, admin_id, assigned_to=theirs.id)
    _emit(db, ticket, admin_id)

    body = "".join(_drain(StubRequest(after=1), mine.id, False, head))

    assert "event: ticket.assigned" not in body
    assert str(ticket.id) not in body


def test_stream_stops_when_the_client_disconnects(db, admin_id):
    """No frame beyond the opener, and the generator returns rather than looping."""
    request = StubRequest(after=0)
    frames = _drain(request, admin_id, True, 0)

    assert len(frames) == 1
    assert request.checks == 1


def test_stream_advances_past_events_it_cannot_show(db, admin_id, make_user):
    """A member's cursor moves over another user's events.

    Without this the tail re-scans from the same id every tick and the scan
    grows without bound on a busy instance.
    """
    watcher = make_user()
    other = make_user()
    head = current_head(db)
    noise = _make_ticket(db, admin_id, assigned_to=other.id)
    _emit(db, noise, admin_id)

    # Two polls: the first must skip the invisible row rather than re-read it.
    request = StubRequest(after=2)
    frames = _drain(request, watcher.id, False, head)

    assert [f for f in frames if f.startswith("id: ")] == []
    assert current_head(db) > head


# --- the HTTP surface -------------------------------------------------------

def test_stream_requires_auth(client):
    assert client.get("/events/stream").status_code == 401


def test_stream_rejects_a_bad_key(client):
    response = client.get("/events/stream", headers={"X-API-Key": "sk_not_a_real_key"})
    assert response.status_code == 401


def test_stream_rejects_a_negative_cursor(client, admin_key):
    response = client.get("/events/stream?last_event_id=-1", headers={"X-API-Key": admin_key})
    assert response.status_code == 422


# --- the handler ------------------------------------------------------------
#
# `TestClient` cannot read this route: it drives the app through a blocking
# portal where `is_disconnected()` never fires, so iterating the body and then
# closing it leaves the generator looping forever and the test hangs. The
# handler is therefore called directly, with the same stub request that lets the
# generator tests terminate. FastAPI's own routing and validation of this path
# are covered by the client tests above.

class StubHttpRequest(StubRequest):
    """A `StubRequest` that also carries headers, as the handler reads those."""

    def __init__(self, headers: dict | None = None, after: int = 0):
        super().__init__(after=after)
        self.headers = Headers(headers or {})


def _open_stream(request, user, last_event_id=None):
    """Call the route handler and return (response, frames it produced)."""
    async def run():
        response = await stream(request, last_event_id=last_event_id, user=user)
        frames = [chunk async for chunk in response.body_iterator]
        return response, frames
    return asyncio.run(run())


@pytest.fixture
def admin_user(db, admin_id):
    return db.query(User).filter(User.id == admin_id).first()


def test_handler_sets_the_streaming_headers(db, admin_user, admin_id):
    ticket = _make_ticket(db, admin_id, assigned_to=admin_id)
    _emit(db, ticket, admin_id)

    response, _ = _open_stream(StubHttpRequest(), admin_user)

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    # nginx buffers proxied bodies by default, which would hold every frame back.
    assert response.headers["x-accel-buffering"] == "no"


def test_handler_starts_at_the_head_by_default(db, admin_user, admin_id):
    """A fresh connection must not replay the whole outbox to every client."""
    ticket = _make_ticket(db, admin_id, assigned_to=admin_id)
    _emit(db, ticket, admin_id)
    head = current_head(db)

    _, frames = _open_stream(StubHttpRequest(), admin_user)

    assert frames == [f": connected at {head}\n\n"]


def test_handler_honors_the_last_event_id_query(db, admin_user):
    _, frames = _open_stream(StubHttpRequest(), admin_user, last_event_id=7)
    assert frames == [": connected at 7\n\n"]


def test_handler_honors_the_last_event_id_header(db, admin_user):
    """EventSource resends `Last-Event-ID` on reconnect; honor it like the query."""
    _, frames = _open_stream(StubHttpRequest({"Last-Event-ID": "11"}), admin_user)
    assert frames == [": connected at 11\n\n"]


def test_handler_ignores_a_junk_last_event_id_header(db, admin_user, admin_id):
    """A malformed header falls back to the head rather than 500ing the stream."""
    head = current_head(db)
    _, frames = _open_stream(StubHttpRequest({"Last-Event-ID": "not-a-number"}), admin_user)
    assert frames == [f": connected at {head}\n\n"]


def test_handler_scopes_the_stream_to_the_caller(db, make_user, admin_id):
    """The member's own identity, not the admin's, decides what the stream shows."""
    member = make_user()
    other = make_user()
    head = current_head(db)
    ticket = _make_ticket(db, admin_id, assigned_to=other.id)
    _emit(db, ticket, admin_id)

    member_user = db.query(User).filter(User.id == member.id).first()
    _, frames = _open_stream(StubHttpRequest(after=1), member_user, last_event_id=head)

    assert "ticket.assigned" not in "".join(frames)
