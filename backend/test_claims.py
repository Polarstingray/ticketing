"""Ticket lease (claim/release/extend) behavior.

The lease exists so two workers can't take the same ticket, and so a worker that
dies mid-ticket doesn't strand it. Those are the two things worth testing, plus
the two things that fall out of them: a released claim is immediately re-takeable,
and an expired one can no longer write results.

TTLs here are the schema minimum (5 s) with a real sleep, which is the honest way
to test expiry — the alternative is freezing the clock, which would test the mock
rather than the query. It costs the suite a handful of seconds in exactly two tests.
"""
import time

import pytest

from database import SessionLocal
from models import SecuritySettings
from schemas import DEFAULT_LEASE_TTL, MIN_LEASE_TTL


def _make_ticket(client, key, **fields) -> int:
    body = {"type": "task", "title": "lease me", "description": "", **fields}
    resp = client.post("/tickets", json=body, headers={"X-API-Key": key})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _claim(client, key, ticket_id, **body):
    return client.post(f"/tickets/{ticket_id}/claim", json=body or {},
                       headers={"X-API-Key": key})


def _release(client, key, ticket_id, token):
    return client.post(f"/tickets/{ticket_id}/release", json={"token": token},
                       headers={"X-API-Key": key})


def _extend(client, key, ticket_id, token, ttl=MIN_LEASE_TTL):
    return client.post(f"/tickets/{ticket_id}/lease/extend",
                       json={"token": token, "ttl_seconds": ttl},
                       headers={"X-API-Key": key})


@pytest.fixture
def worker(make_user):
    """A second worker with the authority to claim: admins may act on any ticket,
    which is what a resolver bot gets by being the ticket's assignee. Using an
    admin keeps these tests about the lease rather than about the ACL."""
    return make_user(role="admin")


# --- two concurrent claims ---------------------------------------------------

def test_second_claim_conflicts(client, admin_key, worker):
    tid = _make_ticket(client, admin_key)

    first = _claim(client, admin_key, tid)
    assert first.status_code == 200, first.text
    lease = first.json()
    assert lease["ticket_id"] == tid
    assert lease["token"]

    second = _claim(client, worker.key, tid)
    assert second.status_code == 409
    # The holder is named, so a worker that loses the race can say who has it.
    assert str(lease["worker_id"]) in second.json()["detail"]


def test_claim_mirrors_a_tag_onto_the_ticket(client, admin_key):
    """The tag is advisory (the row is the source of truth) but it is what a
    human — and the resolver's pre-lease tag logic — reads."""
    tid = _make_ticket(client, admin_key)
    token = _claim(client, admin_key, tid).json()["token"]

    tags = client.get(f"/tickets/{tid}", headers={"X-API-Key": admin_key}).json()["tags"]
    assert "resolver:claimed" in tags

    _release(client, admin_key, tid, token)
    tags = client.get(f"/tickets/{tid}", headers={"X-API-Key": admin_key}).json()["tags"]
    assert "resolver:claimed" not in tags


def test_claim_on_unknown_ticket_is_404(client, admin_key):
    assert _claim(client, admin_key, 10_000_000).status_code == 404


def test_member_cannot_claim_someone_elses_ticket(client, admin_key, make_user):
    """Claiming carries modify authority: otherwise any member could freeze the
    queue out from under the workers that own it."""
    tid = _make_ticket(client, admin_key)
    stranger = make_user()
    assert _claim(client, stranger.key, tid).status_code == 404


def test_deleting_a_ticket_clears_its_lease(client, admin_key):
    """SQLite's FK enforcement is off, so the ORM relationship — not the
    ondelete=CASCADE — is what stops a deleted ticket leaving a lease behind."""
    from database import SessionLocal
    from models import TicketLease

    tid = _make_ticket(client, admin_key)
    _claim(client, admin_key, tid)

    resp = client.delete(f"/tickets/{tid}", headers={"X-API-Key": admin_key})
    assert resp.status_code == 204

    db = SessionLocal()
    try:
        assert db.query(TicketLease).filter(TicketLease.ticket_id == tid).count() == 0
    finally:
        db.close()


# --- expiry and requeue ------------------------------------------------------

def test_expired_lease_is_reclaimable_by_another_worker(client, admin_key, worker):
    tid = _make_ticket(client, admin_key)
    first = _claim(client, admin_key, tid, ttl_seconds=MIN_LEASE_TTL)
    assert first.status_code == 200

    # Still held: the TTL has not run out yet.
    assert _claim(client, worker.key, tid).status_code == 409

    time.sleep(MIN_LEASE_TTL + 1)

    second = _claim(client, worker.key, tid)
    assert second.status_code == 200, second.text
    assert second.json()["worker_id"] == worker.id
    assert second.json()["token"] != first.json()["token"]


def test_extend_keeps_a_lease_alive(client, admin_key, worker):
    tid = _make_ticket(client, admin_key)
    lease = _claim(client, admin_key, tid, ttl_seconds=MIN_LEASE_TTL).json()

    time.sleep(MIN_LEASE_TTL - 2)
    extended = _extend(client, admin_key, tid, lease["token"], ttl=60)
    assert extended.status_code == 200, extended.text
    assert extended.json()["expires_at"] > lease["expires_at"]

    # Past the *original* TTL, the heartbeat has kept the claim exclusive.
    time.sleep(3)
    assert _claim(client, worker.key, tid).status_code == 409


# --- release then reclaim ----------------------------------------------------

def test_release_then_reclaim(client, admin_key, worker):
    tid = _make_ticket(client, admin_key)
    lease = _claim(client, admin_key, tid).json()

    assert _release(client, admin_key, tid, lease["token"]).status_code == 204
    again = _claim(client, worker.key, tid)
    assert again.status_code == 200, again.text
    # And the original holder can take it back once that one lets go.
    assert _release(client, worker.key, tid, again.json()["token"]).status_code == 204
    assert _claim(client, admin_key, tid).status_code == 200


def test_release_with_wrong_token_is_forbidden(client, admin_key, worker):
    """Otherwise anyone who can see the ticket could drop a rival's claim."""
    tid = _make_ticket(client, admin_key)
    lease = _claim(client, admin_key, tid).json()

    assert _release(client, worker.key, tid, "not-the-token").status_code == 403
    # Untouched: the real holder still holds it.
    assert _claim(client, worker.key, tid).status_code == 409
    assert _release(client, admin_key, tid, lease["token"]).status_code == 204


def test_release_without_a_lease_is_404_not_500(client, admin_key):
    tid = _make_ticket(client, admin_key)
    assert _release(client, admin_key, tid, "anything").status_code == 404


# --- an expired lease cannot write results -----------------------------------

def test_expired_lease_cannot_extend_or_write_results(client, admin_key, worker):
    tid = _make_ticket(client, admin_key)
    lease = _claim(client, admin_key, tid, ttl_seconds=MIN_LEASE_TTL).json()

    time.sleep(MIN_LEASE_TTL + 1)

    # An expired lease can't be resurrected by heartbeating late...
    assert _extend(client, admin_key, tid, lease["token"]).status_code == 404
    # ...nor released (there is nothing left to release).
    assert _release(client, admin_key, tid, lease["token"]).status_code == 404

    # ...and results posted against it are refused, even though the poster is
    # still an authorized writer on the ticket.
    run = {"agent": "claude", "phase": "implement", "lease_token": lease["token"]}
    resp = client.post(f"/tickets/{tid}/agent-runs", json=run,
                       headers={"X-API-Key": admin_key})
    assert resp.status_code == 409, resp.text

    # The same write with no lease_token still works — the check is opt-in, so
    # callers that predate the lease are unaffected.
    resp = client.post(f"/tickets/{tid}/agent-runs",
                       json={"agent": "claude", "phase": "implement"},
                       headers={"X-API-Key": admin_key})
    assert resp.status_code == 201, resp.text


def test_another_workers_lease_token_cannot_write_results(client, admin_key, worker):
    tid = _make_ticket(client, admin_key)
    _claim(client, admin_key, tid)

    run = {"agent": "claude", "phase": "implement", "lease_token": "someone-elses"}
    resp = client.post(f"/tickets/{tid}/agent-runs", json=run,
                       headers={"X-API-Key": worker.key})
    assert resp.status_code == 409, resp.text


# --- input bounds ------------------------------------------------------------

def test_ttl_is_bounded(client, admin_key):
    tid = _make_ticket(client, admin_key)
    assert _claim(client, admin_key, tid, ttl_seconds=99_999).status_code == 422
    assert _claim(client, admin_key, tid, ttl_seconds=0).status_code == 422


# --- admin-configured lease TTL policy window ---------------------------------
# The schema's Field(ge=MIN_LEASE_TTL, le=MAX_LEASE_TTL) tested above is the
# absolute hard rail; the security-settings panel lets an admin additionally
# *tighten* the window inside it. That check lives in the claim route itself
# (Pydantic Field bounds are fixed at import time and can't read a DB row).


def _login(c, username: str, password: str):
    r = c.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture(autouse=True)
def _reset_security_settings():
    """This suite shares one DB and one global settings row with
    test_security_settings.py/test_webhooks.py — reset it after every test
    here so a narrowed policy window never leaks into an unrelated test's
    claim() call elsewhere in the suite."""
    yield
    db = SessionLocal()
    try:
        row = db.query(SecuritySettings).filter(SecuritySettings.id == 1).one_or_none()
        if row is not None:
            row.settings = {}
            db.commit()
    finally:
        db.close()


def test_admin_narrowed_window_rejects_ttl_inside_hard_rail_but_outside_policy(
    client, admin_key, new_client,
):
    admin = _login(new_client(), "admin", "admin")
    r = admin.put("/security-settings", json={
        "min_lease_ttl": 100, "max_lease_ttl": 200, "default_lease_ttl": 150,
    })
    assert r.status_code == 200, r.text

    tid = _make_ticket(client, admin_key)
    # 50 is well inside the schema's hard rail (MIN_LEASE_TTL..MAX_LEASE_TTL)
    # but outside the admin-narrowed [100, 200] policy window.
    res = _claim(client, admin_key, tid, ttl_seconds=50)
    assert res.status_code == 422, res.text
    assert "ttl_seconds must be between 100 and 200" in res.text

    ok = _claim(client, admin_key, tid, ttl_seconds=150)
    assert ok.status_code == 200, ok.text


def test_omitted_ttl_uses_admin_configured_default(client, admin_key, new_client):
    """A window that excludes the baked-in DEFAULT_LEASE_TTL (300) but
    includes the admin-configured one (42) — if the route fell back to the
    static default instead of reading the DB one, this claim would 422."""
    assert not (MIN_LEASE_TTL <= DEFAULT_LEASE_TTL <= 100)  # sanity: 300 is outside [5, 100]
    admin = _login(new_client(), "admin", "admin")
    r = admin.put("/security-settings", json={
        "min_lease_ttl": MIN_LEASE_TTL, "max_lease_ttl": 100, "default_lease_ttl": 42,
    })
    assert r.status_code == 200, r.text

    tid = _make_ticket(client, admin_key)
    res = _claim(client, admin_key, tid)  # no ttl_seconds in the body at all
    assert res.status_code == 200, res.text
    assert res.json()["token"]
