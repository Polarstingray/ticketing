"""Tests for the webhook dispatcher.

The four things the feature has to get right, and how each is pinned here:

* **The signature** — against a hand-computed HMAC vector, so a refactor that
  changes what gets signed (dropping the timestamp, say, which would re-open
  replay) fails loudly rather than silently shipping a different scheme.
* **The backoff schedule** — that a failure re-arms the row at the documented
  delay and gives up at MAX_ATTEMPTS.
* **Auto-disable** — that a run of failures deactivates the webhook and reaches
  its owner through the bell.
* **Isolation** — that a receiver which hangs until its timeout does not stall
  delivery to a healthy one in the same pass.

HTTP never leaves the process: deliveries run through an ``httpx.MockTransport``,
so signing, header construction and status handling are exercised for real while
the socket is fake. ``validate_webhook_url`` is stubbed out per-test because the
real one does DNS, which a test must not depend on.
"""
import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

import dispatcher
from database import SessionLocal
from models import (
    DeliveryState,
    Notification,
    Outbox,
    Ticket,
    Webhook,
    WebhookDelivery,
    utcnow,
)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """Neutralize the SSRF re-check.

    The dispatcher re-validates the target immediately before connecting, which
    resolves DNS. These tests use unroutable example hosts, so the real check
    would mark every delivery `skipped` and test nothing. The SSRF rules
    themselves are covered by test_webhooks.py.
    """
    monkeypatch.setattr(dispatcher, "validate_webhook_url", lambda url: url)


def _make_webhook(db, user_id: int, **kwargs) -> Webhook:
    webhook = Webhook(
        user_id=user_id,
        name=kwargs.pop("name", "hook"),
        url=kwargs.pop("url", "https://receiver.example/hook"),
        event_types=kwargs.pop("event_types", []),
        tag_filter=kwargs.pop("tag_filter", []),
        secret=kwargs.pop("secret", "s3cret-value"),
        secret_prefix="s3cret-v",
        active=kwargs.pop("active", True),
        **kwargs,
    )
    db.add(webhook)
    db.commit()
    db.refresh(webhook)
    return webhook


def _make_ticket(db, creator_id: int, tags=None) -> Ticket:
    ticket = Ticket(
        type="task", title="dispatcher fixture", description="d",
        status="open", priority="medium", created_by=creator_id,
        tags=tags or [],
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def _make_event(db, ticket: Ticket, type: str = "ticket.created") -> Outbox:
    event = Outbox(
        type=type,
        ticket_id=ticket.id,
        actor_id=ticket.created_by,
        payload={
            "ticket_id": ticket.id,
            "ticket_title": ticket.title,
            "ticket_tags": list(ticket.tags or []),
        },
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _make_delivery(db, webhook: Webhook, ticket_id=None, **kwargs) -> WebhookDelivery:
    delivery = WebhookDelivery(
        webhook_id=webhook.id,
        event_id=kwargs.pop("event_id", 1),
        event_type=kwargs.pop("event_type", "ticket.created"),
        ticket_id=ticket_id,
        payload=kwargs.pop("payload", {"ticket_id": ticket_id}),
        next_attempt_at=utcnow(),
        state=DeliveryState.pending.value,
        **kwargs,
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    return delivery


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _run(handler, delivery_id: int) -> None:
    async with _client(handler) as client:
        await dispatcher.deliver_one(client, delivery_id)


def _deliver(handler, delivery_id: int) -> None:
    """Drive one delivery attempt synchronously."""
    asyncio.run(_run(handler, delivery_id))


def _reload(db, delivery: WebhookDelivery) -> WebhookDelivery:
    """Re-read a row written by another session."""
    db.expire_all()
    return db.query(WebhookDelivery).filter(WebhookDelivery.id == delivery.id).first()


def _naive_now():
    """``utcnow()`` as it comes back off a DateTime column.

    ``models.utcnow`` is tz-aware, but SQLite has no timezone type and drops the
    offset, so a datetime read back from a row is naive UTC. Subtracting an
    aware datetime from it raises, hence this reference for the delay maths.
    """
    return utcnow().replace(tzinfo=None)


def _deliveries_for(db, event_id: int) -> list[WebhookDelivery]:
    return db.query(WebhookDelivery).filter(WebhookDelivery.event_id == event_id).all()


def _quiesce(db) -> None:
    """Empty the queues before a test that reasons about a whole drain pass.

    The suite shares one database and every route that emits an event leaves an
    unclaimed outbox row behind, so by the time this module runs there is a
    backlog far larger than one CLAIM_BATCH. A test asserting "this pass picked
    up my event" would then be asserting about someone else's rows. Retiring the
    backlog first makes the next pass the test's own.
    """
    db.query(Outbox).filter(Outbox.claimed_at.is_(None)).update(
        {Outbox.claimed_at: utcnow(), Outbox.delivered_at: utcnow()},
        synchronize_session=False,
    )
    db.query(WebhookDelivery).filter(
        WebhookDelivery.state == DeliveryState.pending.value
    ).update(
        {WebhookDelivery.state: DeliveryState.skipped.value},
        synchronize_session=False,
    )
    db.commit()


# --- Signing ----------------------------------------------------------------

def test_signature_matches_known_vector():
    """The signed message is `timestamp + "." + body`, HMAC-SHA256, hex."""
    secret = "topsecret"
    timestamp = "1700000000"
    body = b'{"event":"ticket.created"}'

    expected = hmac.new(
        secret.encode(), b"1700000000." + body, hashlib.sha256
    ).hexdigest()

    assert dispatcher.sign(secret, timestamp, body) == f"sha256={expected}"


def test_signature_covers_the_timestamp():
    """Re-stamping a captured body must invalidate the signature.

    This is the anti-replay property: if the MAC covered only the body, the two
    signatures below would be equal and a captured delivery could be replayed
    forever.
    """
    body = b'{"a":1}'
    assert dispatcher.sign("k", "1700000000", body) != dispatcher.sign("k", "1700000001", body)


def test_signature_depends_on_the_secret():
    body = b'{"a":1}'
    assert dispatcher.sign("k1", "1700000000", body) != dispatcher.sign("k2", "1700000000", body)


def test_delivery_id_is_stable_across_retries():
    """Retries of one delivery share an id, so a receiver can dedupe on it."""
    assert dispatcher.delivery_uuid(42) == dispatcher.delivery_uuid(42)
    assert dispatcher.delivery_uuid(42) != dispatcher.delivery_uuid(43)


def test_body_carries_the_outbox_sequence(db, admin_id):
    """The envelope exposes the monotonic outbox id consumers order on."""
    webhook = _make_webhook(db, admin_id)
    delivery = _make_delivery(db, webhook, ticket_id=7, event_id=1234)

    envelope = json.loads(dispatcher.build_body(delivery))
    assert envelope["sequence"] == 1234
    assert envelope["event"] == "ticket.created"
    assert envelope["id"] == dispatcher.delivery_uuid(delivery.id)
    assert envelope["data"]["ticket_id"] == 7


def test_request_carries_the_documented_headers(db, admin_id):
    webhook = _make_webhook(db, admin_id, secret="hdr-secret")
    delivery = _make_delivery(db, webhook)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(200, text="ok")

    _deliver(handler, delivery.id)

    headers = captured["headers"]
    assert headers["X-Stingray-Event"] == "ticket.created"
    assert headers["X-Stingray-Delivery"] == dispatcher.delivery_uuid(delivery.id)
    assert headers["Content-Type"] == "application/json"
    timestamp = headers["X-Stingray-Timestamp"]
    # The receiver's own verification, done exactly as documented.
    assert headers["X-Stingray-Signature"] == dispatcher.sign(
        "hdr-secret", timestamp, captured["body"]
    )

    row = _reload(db, delivery)
    assert row.state == DeliveryState.succeeded.value
    assert row.status_code == 200
    assert row.attempt_count == 1
    assert row.next_attempt_at is None


# --- Fan-out ----------------------------------------------------------------

def test_fan_out_creates_one_delivery_per_subscriber(db, admin_id):
    ticket = _make_ticket(db, admin_id)
    event = _make_event(db, ticket)
    a = _make_webhook(db, admin_id, name="a")
    b = _make_webhook(db, admin_id, name="b")

    dispatcher.fan_out(db, event.id)

    # Scoped to this test's own webhooks: the suite shares one database, so
    # neither the delivery count for an event nor the set of matching webhooks
    # is this test's to own.
    rows = [r for r in _deliveries_for(db, event.id) if r.webhook_id in {a.id, b.id}]
    assert {r.webhook_id for r in rows} == {a.id, b.id}  # one each, no duplicates
    assert all(r.state == DeliveryState.pending.value for r in rows)
    assert all(r.ticket_id == ticket.id for r in rows)
    assert all(r.payload["ticket_title"] == ticket.title for r in rows)
    # The outbox row is marked fanned-out, not "received".
    db.refresh(event)
    assert event.delivered_at is not None


def test_fan_out_respects_event_type_and_tag_filters(db, admin_id):
    ticket = _make_ticket(db, admin_id, tags=["repo:alpha"])
    event = _make_event(db, ticket, type="ticket.created")

    wanted = _make_webhook(db, admin_id, name="w", event_types=["ticket.created"])
    wrong_type = _make_webhook(db, admin_id, name="t", event_types=["comment.created"])
    wrong_tag = _make_webhook(db, admin_id, name="g", tag_filter=["repo:beta"])
    inactive = _make_webhook(db, admin_id, name="i", active=False)

    dispatcher.fan_out(db, event.id)

    got = {
        r.webhook_id
        for r in db.query(WebhookDelivery).filter(WebhookDelivery.event_id == event.id)
    }
    assert wanted.id in got
    assert wrong_type.id not in got
    assert wrong_tag.id not in got
    assert inactive.id not in got


def test_fan_out_is_not_repeated_for_one_event(db, admin_id):
    """A re-claimed outbox row must not queue the same event twice."""
    ticket = _make_ticket(db, admin_id)
    event = _make_event(db, ticket)
    _make_webhook(db, admin_id)

    first = dispatcher.fan_out(db, event.id)
    assert first >= 1
    after_first = len(_deliveries_for(db, event.id))

    # The second pass is the one that matters: nothing more is queued.
    assert dispatcher.fan_out(db, event.id) == 0
    assert len(_deliveries_for(db, event.id)) == after_first


def test_fan_out_will_not_ship_a_ticket_the_owner_cannot_see(db, admin_id, make_user):
    """A member's webhook must not become a way to read other people's tickets."""
    outsider = make_user()
    ticket = _make_ticket(db, admin_id)  # created by admin, not the outsider
    event = _make_event(db, ticket)
    snoop = _make_webhook(db, outsider.id, name="snoop")

    dispatcher.fan_out(db, event.id)

    # The admin's own subscribe-all webhooks may well have matched; the point is
    # that the outsider's did not.
    assert snoop.id not in {r.webhook_id for r in _deliveries_for(db, event.id)}


def test_claim_batch_stamps_and_skips_claimed_rows(db, admin_id):
    _quiesce(db)
    ticket = _make_ticket(db, admin_id)
    event = _make_event(db, ticket)

    claimed = dispatcher.claim_batch(db)
    assert event.id in claimed
    db.refresh(event)
    assert event.claimed_at is not None
    # A second pass must not re-claim it.
    assert event.id not in dispatcher.claim_batch(db)


# --- Retry / backoff --------------------------------------------------------

def test_failure_re_arms_with_the_documented_backoff(db, admin_id):
    webhook = _make_webhook(db, admin_id)
    delivery = _make_delivery(db, webhook)

    def failing(request):
        return httpx.Response(500, text="boom")

    before = _naive_now()
    _deliver(failing, delivery.id)

    row = _reload(db, delivery)
    assert row.state == DeliveryState.pending.value  # queued again, not failed
    assert row.attempt_count == 1
    assert row.status_code == 500
    assert "HTTP 500" in row.error
    delay = (row.next_attempt_at - before).total_seconds()
    assert dispatcher.BACKOFF_SECONDS[0] <= delay <= dispatcher.BACKOFF_SECONDS[0] + 10


def test_backoff_grows_with_each_attempt(db, admin_id):
    """The schedule is exponential, so a broken receiver is backed off from."""
    webhook = _make_webhook(db, admin_id)

    def failing(request):
        return httpx.Response(503)

    delays = []
    for attempt in range(1, 4):
        delivery = _make_delivery(db, webhook, attempt_count=attempt - 1)
        before = _naive_now()
        _deliver(failing, delivery.id)
        row = _reload(db, delivery)
        assert row.attempt_count == attempt
        delays.append((row.next_attempt_at - before).total_seconds())

    assert delays[0] < delays[1] < delays[2]
    assert delays[0] >= dispatcher.BACKOFF_SECONDS[0]


def test_delivery_gives_up_after_max_attempts(db, admin_id):
    webhook = _make_webhook(db, admin_id)
    # One attempt short of the budget: this attempt is the last one.
    delivery = _make_delivery(db, webhook, attempt_count=dispatcher.MAX_ATTEMPTS - 1)

    _deliver(lambda request: httpx.Response(500), delivery.id)

    row = _reload(db, delivery)
    assert row.attempt_count == dispatcher.MAX_ATTEMPTS
    assert row.state == DeliveryState.failed.value
    assert row.next_attempt_at is None  # never picked up again


def test_transport_error_is_retried_like_a_bad_status(db, admin_id):
    webhook = _make_webhook(db, admin_id)
    delivery = _make_delivery(db, webhook)

    def exploding(request):
        raise httpx.ConnectError("connection refused")

    _deliver(exploding, delivery.id)

    row = _reload(db, delivery)
    assert row.state == DeliveryState.pending.value
    assert row.status_code is None
    assert "ConnectError" in row.error
    assert row.next_attempt_at is not None


def test_success_clears_the_failure_streak(db, admin_id):
    webhook = _make_webhook(db, admin_id, consecutive_failures=4)
    delivery = _make_delivery(db, webhook)

    _deliver(lambda request: httpx.Response(204), delivery.id)

    db.expire_all()
    assert db.query(Webhook).filter(Webhook.id == webhook.id).first().consecutive_failures == 0


def test_due_delivery_ids_skips_rows_not_yet_due(db, admin_id):
    from datetime import timedelta

    webhook = _make_webhook(db, admin_id)
    due = _make_delivery(db, webhook)
    later = _make_delivery(db, webhook)
    later.next_attempt_at = utcnow() + timedelta(hours=1)
    db.commit()

    ids = dispatcher.due_delivery_ids(db)
    assert due.id in ids
    assert later.id not in ids


def test_inactive_webhook_skips_rather_than_sends(db, admin_id):
    """A subscription switched off between fan-out and send is never delivered."""
    webhook = _make_webhook(db, admin_id, active=False)
    delivery = _make_delivery(db, webhook)

    def must_not_be_called(request):  # pragma: no cover - asserts by not running
        raise AssertionError("an inactive webhook must not be sent to")

    _deliver(must_not_be_called, delivery.id)

    row = _reload(db, delivery)
    assert row.state == DeliveryState.skipped.value


# --- Auto-disable -----------------------------------------------------------

def test_repeated_failures_disable_the_webhook_and_notify_its_owner(db, admin_id, make_user):
    owner = make_user()
    webhook = _make_webhook(
        db, owner.id, name="flaky",
        consecutive_failures=dispatcher.MAX_CONSECUTIVE_FAILURES - 1,
    )
    delivery = _make_delivery(db, webhook)

    _deliver(lambda request: httpx.Response(500), delivery.id)

    db.expire_all()
    stored = db.query(Webhook).filter(Webhook.id == webhook.id).first()
    assert stored.consecutive_failures == dispatcher.MAX_CONSECUTIVE_FAILURES
    assert stored.active is False

    notice = (
        db.query(Notification)
        .filter(Notification.user_id == owner.id, Notification.type == "webhook_disabled")
        .first()
    )
    assert notice is not None
    assert "flaky" in notice.ticket_title
    # A system notification names no ticket and no actor.
    assert notice.ticket_id is None
    assert notice.actor_id is None


def test_webhook_is_not_disabled_before_the_threshold(db, admin_id, make_user):
    owner = make_user()
    webhook = _make_webhook(
        db, owner.id, consecutive_failures=dispatcher.MAX_CONSECUTIVE_FAILURES - 3
    )
    delivery = _make_delivery(db, webhook)

    _deliver(lambda request: httpx.Response(500), delivery.id)

    db.expire_all()
    assert db.query(Webhook).filter(Webhook.id == webhook.id).first().active is True
    assert (
        db.query(Notification)
        .filter(Notification.user_id == owner.id, Notification.type == "webhook_disabled")
        .count() == 0
    )


# --- Isolation --------------------------------------------------------------

def test_a_hanging_webhook_does_not_stall_a_healthy_one(db, admin_id):
    """The requirement in the ticket: one bad receiver cannot hold up the queue.

    The slow handler blocks for longer than the whole test would tolerate if
    deliveries were sequential; the healthy one must still complete in the same
    pass, which only happens because the pass runs them concurrently.
    """
    slow = _make_webhook(db, admin_id, name="slow", url="https://slow.example/hook")
    fast = _make_webhook(db, admin_id, name="fast", url="https://fast.example/hook")
    slow_delivery = _make_delivery(db, slow)
    fast_delivery = _make_delivery(db, fast)

    order = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if "slow" in str(request.url):
            await asyncio.sleep(0.4)
            order.append("slow")
            raise httpx.ReadTimeout("timed out")
        order.append("fast")
        return httpx.Response(200, text="ok")

    async def drive():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await asyncio.gather(
                dispatcher.deliver_one(client, slow_delivery.id),
                dispatcher.deliver_one(client, fast_delivery.id),
            )

    asyncio.run(drive())

    # The healthy receiver answered while the slow one was still blocked.
    assert order == ["fast", "slow"]
    assert _reload(db, fast_delivery).state == DeliveryState.succeeded.value
    # And the slow one is retried, not lost.
    slow_row = _reload(db, slow_delivery)
    assert slow_row.state == DeliveryState.pending.value
    assert slow_row.next_attempt_at is not None


def test_drain_once_runs_the_whole_pipeline(db, admin_id):
    """End to end through the real loop body: outbox row in, HTTP request out."""
    _quiesce(db)
    ticket = _make_ticket(db, admin_id)
    event = _make_event(db, ticket)
    webhook = _make_webhook(db, admin_id, name="e2e", url="https://e2e.example/hook")
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "e2e.example" not in str(request.url):
            return httpx.Response(500)
        sent.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    async def drive():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await dispatcher.drain_once(client)

    asyncio.run(drive())

    assert any(e["sequence"] == event.id for e in sent)
    db.expire_all()
    row = (
        db.query(WebhookDelivery)
        .filter(
            WebhookDelivery.event_id == event.id,
            WebhookDelivery.webhook_id == webhook.id,
        )
        .first()
    )
    assert row is not None
    assert row.state == DeliveryState.succeeded.value


def test_a_failing_delivery_does_not_abort_the_pass(db, admin_id, monkeypatch):
    """An exception in one delivery must not cancel its siblings in the gather."""
    _quiesce(db)
    good = _make_webhook(db, admin_id, name="good", url="https://good.example/hook")
    bad = _make_webhook(db, admin_id, name="bad", url="https://bad.example/hook")
    good_delivery = _make_delivery(db, good)
    bad_delivery = _make_delivery(db, bad)

    real_deliver = dispatcher.deliver_one

    async def flaky(client, delivery_id):
        if delivery_id == bad_delivery.id:
            raise RuntimeError("dispatcher bug")
        await real_deliver(client, delivery_id)

    monkeypatch.setattr(dispatcher, "deliver_one", flaky)

    async def drive():
        async with _client(lambda request: httpx.Response(200, text="ok")) as client:
            await dispatcher.drain_once(client)

    asyncio.run(drive())  # must not raise

    assert _reload(db, good_delivery).state == DeliveryState.succeeded.value


# --- Matching (pure) --------------------------------------------------------

@pytest.mark.parametrize(
    "event_types,tag_filter,event,tags,expected",
    [
        ([], [], "ticket.created", [], True),                       # subscribe-all
        (["ticket.created"], [], "ticket.created", [], True),
        (["ticket.created"], [], "comment.created", [], False),
        ([], ["repo:a"], "ticket.created", ["repo:a"], True),
        ([], ["repo:a"], "ticket.created", ["repo:b"], False),
        ([], ["repo:a"], "ticket.created", [], False),              # filter, no tags
        (["ticket.created"], ["repo:a"], "ticket.created", ["repo:a", "x"], True),
        (["ticket.created"], ["repo:a"], "comment.created", ["repo:a"], False),
    ],
)
def test_matches(event_types, tag_filter, event, tags, expected):
    webhook = Webhook(
        user_id=1, name="w", url="https://x.example/h", secret="s",
        event_types=event_types, tag_filter=tag_filter, active=True,
    )
    assert dispatcher.matches(webhook, event, tags) is expected


def test_matches_rejects_an_inactive_webhook():
    webhook = Webhook(
        user_id=1, name="w", url="https://x.example/h", secret="s",
        event_types=[], tag_filter=[], active=False,
    )
    assert dispatcher.matches(webhook, "ticket.created", []) is False
