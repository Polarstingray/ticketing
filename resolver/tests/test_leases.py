"""The sweep's use of the server-side ticket lease.

The lease is the race guard: `sweep` claims a ticket before touching it, so a
second sweep (or a third-party agent) working the same queue is turned away by
the database rather than duplicating the work. These tests drive `sweep` with a
stubbed claim endpoint, since what matters is the branching around it — claimed,
contended, and "the server doesn't do leases".
"""
import pytest
import requests

import resolve_tickets as rt
from stingray_client.api import StingrayClient


class FakeLeaseClient:
    """Stands in for the per-ticket client `acquire_lease` builds.

    ``grants`` maps ticket id -> the token to hand out, or None to answer 409
    (someone else holds it).
    """

    def __init__(self, grants: dict, raise_on_claim: Exception | None = None):
        self.grants = grants
        self.raise_on_claim = raise_on_claim
        self.claimed: list[int] = []
        self.released: list[tuple[int, str]] = []
        self.extended: list[int] = []

    def claim_ticket(self, ticket_id, ttl_seconds=300):
        if self.raise_on_claim is not None:
            raise self.raise_on_claim
        self.claimed.append(ticket_id)
        token = self.grants.get(ticket_id)
        if token is None:
            return None
        return {"ticket_id": ticket_id, "worker_id": 2, "token": token,
                "expires_at": "2099-01-01T00:00:00+00:00"}

    def extend_lease(self, ticket_id, token, ttl_seconds=300):
        self.extended.append(ticket_id)
        return {"ticket_id": ticket_id, "token": token}

    def release_ticket(self, ticket_id, token):
        self.released.append((ticket_id, token))
        return True


@pytest.fixture
def lease_client(monkeypatch):
    """Install a FakeLeaseClient factory in place of the real StingrayClient that
    `acquire_lease` constructs, and hand the instance back to the test."""
    holder = {}

    def _install(grants=None, raise_on_claim=None):
        fake = FakeLeaseClient(grants or {}, raise_on_claim)
        monkeypatch.setattr(rt, "StingrayClient",
                            lambda *a, **k: fake)
        holder["fake"] = fake
        return fake

    return _install


@pytest.fixture
def recorded_process(monkeypatch):
    """Replace `process` with a recorder, so these tests are about the claim
    branching rather than about the (heavily tested) phase machinery."""
    seen: list[int] = []
    monkeypatch.setattr(
        rt, "process",
        lambda cfg, client, ticket, dry_run: seen.append(ticket["id"]))
    return seen


def _ticket(tid: int) -> dict:
    from conftest import BOT
    return {"id": tid, "title": f"t{tid}", "status": "open", "tags": [],
            "created_by": 9, "type": "task", "description": "", "assigned_to": BOT}


# --- set_state -----------------------------------------------------------

def test_set_state_preserves_the_claim_mirror(monkeypatch):
    """A phase transition rewrites the resolver:* tags, but the claim mirror is
    not a phase — clearing it would advertise the ticket as free while this
    worker still holds the lease."""
    sent = {}

    class C:
        def update_ticket(self, ticket_id, **fields):
            sent.update(fields)
            return {"id": ticket_id}

    ticket = {"id": 1, "tags": ["repo:app", rt.TAG_CLAIMED, rt.TAG_PLANNING]}
    rt.set_state(C(), ticket, [rt.TAG_IMPLEMENTING])

    assert rt.TAG_CLAIMED in sent["tags"]
    assert rt.TAG_IMPLEMENTING in sent["tags"]
    assert "repo:app" in sent["tags"]
    assert rt.TAG_PLANNING not in sent["tags"]


def test_the_claim_mirror_is_not_a_workflow_tag():
    """`process` dispatches on the resolver tags, and treats an empty set as
    "fresh ticket". The mirror must not make a fresh ticket look mid-flight."""
    assert rt.resolver_tags({"tags": [rt.TAG_CLAIMED, "repo:app"]}) == set()
    assert rt.resolver_tags(
        {"tags": [rt.TAG_CLAIMED, rt.TAG_PLANNING]}) == {rt.TAG_PLANNING}


# --- sweep ---------------------------------------------------------------

def test_sweep_claims_processes_and_releases(fake_cfg, lease_client,
                                             recorded_process):
    from conftest import FakeClient

    fake = lease_client(grants={7: "tok-7"})
    client = FakeClient(tickets=[_ticket(7)])

    processed, skipped = rt.sweep(fake_cfg, client, dry_run=False, only=None)
    assert processed == 1
    assert skipped == 0
    assert recorded_process == [7]
    assert fake.claimed == [7]
    # Released on the way out, so the ticket is workable again immediately
    # rather than after a TTL — this is also what covers the failure paths.
    assert fake.released == [(7, "tok-7")]
    assert rt.lease_token_for(7) is None


def test_sweep_skips_a_ticket_another_worker_holds(fake_cfg, lease_client,
                                                   recorded_process):
    from conftest import FakeClient

    # 8 is contended (no grant); 9 is free.
    fake = lease_client(grants={9: "tok-9"})
    client = FakeClient(tickets=[_ticket(8), _ticket(9)])

    processed, skipped = rt.sweep(fake_cfg, client, dry_run=False, only=None)

    assert recorded_process == [9]
    # A skipped ticket isn't "processed" — nothing was done with it, so it must
    # not eat a slot in a bounded sweep either.
    assert processed == 1
    assert skipped == 1
    assert fake.released == [(9, "tok-9")]


def test_a_skipped_ticket_does_not_consume_max_tickets(fake_cfg, lease_client,
                                                       recorded_process):
    from conftest import FakeClient

    lease_client(grants={11: "tok-11"})  # 10 contended, 11 free
    client = FakeClient(tickets=[_ticket(10), _ticket(11)])

    processed, skipped = rt.sweep(fake_cfg, client, dry_run=False, only=None, max_tickets=1)
    assert processed == 1
    assert skipped == 1
    assert recorded_process == [11]


def test_sweep_proceeds_unleased_when_the_server_has_no_lease_api(
        fake_cfg, lease_client, recorded_process):
    """Deployability: a backend that predates the lease endpoints must not stop
    the resolver working. Only a 409 means contention; an error means "carry on
    as before", which is exactly the pre-lease behavior."""
    from conftest import FakeClient

    fake = lease_client(raise_on_claim=requests.HTTPError("404"))
    client = FakeClient(tickets=[_ticket(12)])

    processed, skipped = rt.sweep(fake_cfg, client, dry_run=False, only=None)
    assert processed == 1
    assert skipped == 0
    assert recorded_process == [12]
    assert fake.released == []
    assert rt.lease_token_for(12) is None


def test_sweep_releases_even_when_processing_blows_up(fake_cfg, lease_client,
                                                      monkeypatch):
    """A crash mid-ticket must not strand the claim — that stranding is the bug
    the TTL exists for, and releasing eagerly means not even waiting one TTL."""
    from conftest import FakeClient

    fake = lease_client(grants={13: "tok-13"})

    def boom(cfg, client, ticket, dry_run):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(rt, "process", boom)
    monkeypatch.setattr(rt, "fail", lambda *a, **k: None)
    client = FakeClient(tickets=[_ticket(13)])

    rt.sweep(fake_cfg, client, dry_run=False, only=None)
    assert fake.released == [(13, "tok-13")]


def test_dry_run_never_claims(fake_cfg, lease_client, recorded_process):
    """--dry-run reports what a sweep would do; taking a real claim would block
    the sweep that actually does it."""
    from conftest import FakeClient

    fake = lease_client(grants={14: "tok-14"})
    client = FakeClient(tickets=[_ticket(14)])

    rt.sweep(fake_cfg, client, dry_run=True, only=None)
    assert recorded_process == [14]
    assert fake.claimed == []


def test_single_ticket_run_claims_too(fake_cfg, lease_client, recorded_process):
    from conftest import FakeClient

    fake = lease_client(grants={15: "tok-15"})
    client = FakeClient(tickets=[_ticket(15)])

    processed, skipped = rt.sweep(fake_cfg, client, dry_run=False, only=15)
    assert processed == 1
    assert skipped == 0
    assert recorded_process == [15]
    assert fake.released == [(15, "tok-15")]


def test_single_ticket_run_yields_to_the_holder(fake_cfg, lease_client,
                                                recorded_process):
    from conftest import FakeClient

    lease_client(grants={})
    client = FakeClient(tickets=[_ticket(16)])

    processed, skipped = rt.sweep(fake_cfg, client, dry_run=False, only=16)
    assert processed == 0
    assert skipped == 0
    assert recorded_process == []


# --- lease token plumbing ------------------------------------------------

def test_lease_token_is_visible_while_the_ticket_is_being_processed(
        fake_cfg, lease_client, monkeypatch):
    """Agent-run writes attach `lease_token_for(id)` so the server can refuse
    results from a worker whose claim has lapsed."""
    from conftest import FakeClient

    seen = {}
    lease_client(grants={17: "tok-17"})
    monkeypatch.setattr(
        rt, "process",
        lambda cfg, client, ticket, dry_run: seen.setdefault(
            "token", rt.lease_token_for(ticket["id"])))

    rt.sweep(fake_cfg, FakeClient(tickets=[_ticket(17)]), dry_run=False, only=None)
    assert seen["token"] == "tok-17"
    # ...and gone once the lease is handed back.
    assert rt.lease_token_for(17) is None


# --- the API client's lease methods --------------------------------------

class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


def _client(monkeypatch, resp):
    client = StingrayClient("http://x", "k", max_retries=1)
    monkeypatch.setattr(client.session, "request",
                        lambda *a, **k: resp)
    return client


def test_claim_returns_none_on_conflict(monkeypatch):
    assert _client(monkeypatch, _Resp(409)).claim_ticket(1) is None


def test_claim_returns_the_lease(monkeypatch):
    lease = {"ticket_id": 1, "token": "t", "worker_id": 2,
             "expires_at": "2099-01-01T00:00:00+00:00"}
    assert _client(monkeypatch, _Resp(200, lease)).claim_ticket(1) == lease


def test_claim_still_raises_on_a_real_error(monkeypatch):
    with pytest.raises(requests.HTTPError):
        _client(monkeypatch, _Resp(403)).claim_ticket(1)


def test_extend_returns_none_when_the_lease_is_gone(monkeypatch):
    assert _client(monkeypatch, _Resp(404)).extend_lease(1, "t") is None


def test_release_is_false_when_there_was_nothing_to_release(monkeypatch):
    """Release runs in a `finally`; a lease that already expired is an ordinary
    outcome there, not something to raise through the cleanup path."""
    assert _client(monkeypatch, _Resp(404)).release_ticket(1, "t") is False
    assert _client(monkeypatch, _Resp(204)).release_ticket(1, "t") is True
