"""Tests for the transactional outbox event bus (``events.emit``).

Two halves: the unit contract of ``emit`` — that it stages a row and nothing
more, so it commits and rolls back with the caller's transaction — and the
route seams, checked by reading the ``outbox`` table directly since no endpoint
exposes it yet.
"""
import pytest

from database import SessionLocal
from events import emit
from models import Outbox, Ticket, User


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _make_ticket(db, creator_id: int, title: str = "outbox fixture") -> Ticket:
    ticket = Ticket(
        type="task", title=title, description="d",
        status="open", priority="medium", created_by=creator_id,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def _events(db, ticket_id: int, type: str | None = None) -> list[Outbox]:
    q = db.query(Outbox).filter(Outbox.ticket_id == ticket_id)
    if type is not None:
        q = q.filter(Outbox.type == type)
    return q.order_by(Outbox.id.asc()).all()


# --- emit() contract --------------------------------------------------------

def test_emit_stages_a_row(db, admin_id):
    actor = db.query(User).filter(User.id == admin_id).first()
    ticket = _make_ticket(db, admin_id)

    emit(db, type="ticket.created", ticket=ticket, actor=actor)
    db.commit()

    rows = _events(db, ticket.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.type == "ticket.created"
    assert row.actor_id == admin_id
    assert row.created_at is not None
    # Dispatcher bookkeeping stays NULL until a dispatcher claims the row.
    assert row.claimed_at is None
    assert row.delivered_at is None
    assert row.payload["ticket_id"] == ticket.id
    assert row.payload["ticket_title"] == ticket.title
    assert row.payload["actor_id"] == admin_id
    assert row.payload["actor_name"] == actor.display_name
    # No delta was passed, so the key is absent rather than null.
    assert "delta" not in row.payload


def test_emit_is_rolled_back_with_the_caller(db, admin_id):
    """The whole point of the outbox: no phantom event for an aborted change."""
    actor = db.query(User).filter(User.id == admin_id).first()
    ticket = _make_ticket(db, admin_id)

    emit(db, type="ticket.created", ticket=ticket, actor=actor)
    db.rollback()

    assert _events(db, ticket.id) == []


def test_emit_records_delta(db, admin_id):
    actor = db.query(User).filter(User.id == admin_id).first()
    ticket = _make_ticket(db, admin_id)

    emit(db, type="ticket.status_changed", ticket=ticket, actor=actor,
         delta={"from": "open", "to": "resolved"})
    db.commit()

    rows = _events(db, ticket.id)
    assert len(rows) == 1
    assert rows[0].payload["delta"] == {"from": "open", "to": "resolved"}


def test_emit_without_an_actor(db, admin_id):
    ticket = _make_ticket(db, admin_id)

    emit(db, type="ticket.status_changed", ticket=ticket, actor=None)
    db.commit()

    rows = _events(db, ticket.id)
    assert len(rows) == 1
    assert rows[0].actor_id is None
    assert rows[0].payload["actor_id"] is None
    assert rows[0].payload["actor_name"] is None


def test_ids_are_monotonic(db, admin_id):
    """`id` doubles as the sequence consumers order on."""
    actor = db.query(User).filter(User.id == admin_id).first()
    ticket = _make_ticket(db, admin_id)

    emit(db, type="ticket.created", ticket=ticket, actor=actor)
    emit(db, type="comment.created", ticket=ticket, actor=actor)
    db.commit()

    ids = [r.id for r in _events(db, ticket.id)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 2


# --- route seams ------------------------------------------------------------

def _post_ticket(client, admin_key, **extra) -> int:
    body = {"type": "task", "title": "seam ticket", "description": "d",
            "priority": "medium"}
    body.update(extra)
    r = client.post("/tickets", json=body, headers={"X-API-Key": admin_key})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_create_ticket_emits_ticket_created(client, admin_key, db):
    ticket_id = _post_ticket(client, admin_key)

    rows = _events(db, ticket_id, "ticket.created")
    assert len(rows) == 1
    assert rows[0].payload["ticket_title"] == "seam ticket"


def test_create_assigned_ticket_also_emits_ticket_assigned(
    client, admin_key, admin_id, db
):
    ticket_id = _post_ticket(client, admin_key, assigned_to=admin_id)

    assert len(_events(db, ticket_id, "ticket.created")) == 1
    rows = _events(db, ticket_id, "ticket.assigned")
    assert len(rows) == 1
    assert rows[0].payload["delta"]["to"] == admin_id


def test_comment_emits_comment_created(client, admin_key, db):
    ticket_id = _post_ticket(client, admin_key)
    r = client.post(f"/tickets/{ticket_id}/comments", json={"body": "hi"},
                    headers={"X-API-Key": admin_key})
    assert r.status_code == 201, r.text

    rows = _events(db, ticket_id, "comment.created")
    assert len(rows) == 1
    assert rows[0].payload["delta"]["comment_id"] == r.json()["id"]


def test_status_change_emits_with_delta(client, admin_key, db):
    ticket_id = _post_ticket(client, admin_key)
    r = client.patch(f"/tickets/{ticket_id}", json={"status": "resolved"},
                     headers={"X-API-Key": admin_key})
    assert r.status_code == 200, r.text

    rows = _events(db, ticket_id, "ticket.status_changed")
    assert len(rows) == 1
    assert rows[0].payload["delta"] == {"from": "open", "to": "resolved"}


def test_unchanged_status_emits_nothing(client, admin_key, db):
    """A no-op PATCH must not manufacture an event."""
    ticket_id = _post_ticket(client, admin_key)
    r = client.patch(f"/tickets/{ticket_id}", json={"status": "open"},
                     headers={"X-API-Key": admin_key})
    assert r.status_code == 200, r.text

    assert _events(db, ticket_id, "ticket.status_changed") == []


def test_tag_change_emits_ticket_tagged(client, admin_key, db):
    ticket_id = _post_ticket(client, admin_key)
    r = client.patch(f"/tickets/{ticket_id}", json={"tags": ["alpha"]},
                     headers={"X-API-Key": admin_key})
    assert r.status_code == 200, r.text

    rows = _events(db, ticket_id, "ticket.tagged")
    assert len(rows) == 1
    assert rows[0].payload["delta"] == {"added": ["alpha"], "removed": []}


def test_agent_run_emits_finished(client, admin_key, admin_id, db):
    ticket_id = _post_ticket(client, admin_key, assigned_to=admin_id)
    r = client.post(
        f"/tickets/{ticket_id}/agent-runs",
        json={"agent": "claude", "phase": "implement", "model": "opus",
              "status": "succeeded"},
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 201, r.text

    rows = _events(db, ticket_id, "agent_run.finished")
    assert len(rows) == 1
    assert rows[0].payload["delta"]["phase"] == "implement"


def test_failed_update_leaves_no_event(client, admin_key, db):
    """A rejected PATCH rolls back, so no `ticket.assigned` is left behind."""
    ticket_id = _post_ticket(client, admin_key)
    r = client.patch(f"/tickets/{ticket_id}", json={"assigned_to": 10_000_000},
                     headers={"X-API-Key": admin_key})
    assert r.status_code == 400

    assert _events(db, ticket_id, "ticket.assigned") == []
