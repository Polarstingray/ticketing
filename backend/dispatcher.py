"""The webhook dispatcher: drains the outbox and delivers signed HTTP events.

``events.emit`` stages an ``Outbox`` row inside the transaction that made the
change it describes. This module is the other half — the background task that
picks those rows up, fans each one out to the webhooks subscribed to it, and
makes the actual HTTP request.

The delivery contract (what a consumer may assume)
--------------------------------------------------
* **At-least-once.** An event is retried until it is accepted or the attempt
  budget runs out, and a crash between "sent" and "recorded" re-sends. A
  receiver **must** be idempotent. ``X-Stingray-Delivery`` is stable across the
  retries of one delivery, so it is the key to dedupe on.
* **No ordering guarantee.** None at all, and especially not across retries: a
  failing event backs off for hours while later events sail through, so
  ``ticket.status_changed`` routinely arrives before the ``ticket.created`` it
  followed. The body carries ``sequence`` (the monotonic ``outbox.id``) so a
  consumer that cares can order or discard by it.
* **The payload is a hint, not truth.** It is a snapshot from emit time and the
  ticket has very likely moved on by the time it is read. Consumers are told to
  **re-fetch the ticket** rather than trust the body.

The SQLite constraint
---------------------
SQLite takes one writer at a time, so the cardinal rule here is that **a write
transaction is never held across an HTTP request**. Every phase opens its own
short session, commits, and closes before any network I/O starts:

    claim a batch  → commit → fan out → commit → send → commit the outcome

The loop sleeps :data:`DRAIN_INTERVAL` between passes rather than busy-looping,
and ``database.py`` puts the engine in WAL with a busy timeout so a drain and a
request-path write don't knock each other over.

Concurrency inside a pass is deliberate: deliveries are sent with
``asyncio.gather`` under a semaphore, so one receiver that black-holes the
connection until its timeout cannot stall delivery to a healthy one.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import timedelta
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

import inbox
from database import SessionLocal
from models import (
    MAX_RESPONSE_SNIPPET,
    DeliveryState,
    Outbox,
    Ticket,
    User,
    Webhook,
    WebhookDelivery,
    utcnow,
)
from webhook_urls import validate_webhook_url

log = logging.getLogger("stingray.dispatcher")

# Seconds between drain passes. Deliberately not a busy loop — see the module
# docstring on write contention.
DRAIN_INTERVAL = 1.0

# Outbox rows claimed per pass, and deliveries attempted per pass. Bounded so a
# backlog is worked off steadily instead of in one burst of sockets.
CLAIM_BATCH = 50
DELIVER_BATCH = 50

# Concurrent in-flight HTTP requests. This is what keeps a slow receiver from
# stalling the queue behind it.
MAX_CONCURRENT_DELIVERIES = 8

# Per-request timeout. Must stay well under DRAIN_INTERVAL * a small factor is
# *not* required — deliveries run concurrently with the next pass's sleep — but
# a receiver gets a bounded amount of our time either way.
REQUEST_TIMEOUT = 10.0

# Attempts per delivery before it is given up on as `failed`. The backoff below
# spans roughly nine hours, which is long enough to ride out a receiver's
# deploy without holding a row forever.
MAX_ATTEMPTS = 5

# Delay before attempt N+1, indexed by the attempt that just failed (1-based).
# The last value repeats if it is ever indexed past the end.
BACKOFF_SECONDS = [60, 300, 1800, 7200, 21600]

# Consecutive failures — counted on the *webhook*, across deliveries — after
# which it is switched off and its owner told. A receiver that has been broken
# this consistently is not coming back on its own, and continuing to hammer it
# is what turns one dead endpoint into a backlog.
MAX_CONSECUTIVE_FAILURES = 10

# Namespace for the per-delivery UUID. Fixed, so the id is derived from the
# delivery row rather than generated per attempt — retries of one delivery
# therefore share an id and a consumer can dedupe on it.
DELIVERY_NAMESPACE = uuid.UUID("6f3c9a1e-0b64-4f5b-9d2a-8c7e1f4a5b30")


def enabled() -> bool:
    """Whether the lifespan should start the dispatcher (default on).

    Read per-call rather than at import so the test suite can switch it off
    before the app starts, and so an operator has a kill switch that does not
    require a code change.
    """
    return os.environ.get("DISPATCHER_ENABLED", "1") != "0"


# --- Signing -----------------------------------------------------------------

def sign(secret: str, timestamp: str, body: bytes) -> str:
    """The ``X-Stingray-Signature`` value for ``body`` at ``timestamp``.

    GitHub-shaped (``sha256=<hex>``) so an existing handler works unchanged, but
    the signed message is ``timestamp + "." + body``, not the body alone. That
    is the whole point of including the timestamp: signing only the body means a
    captured delivery stays valid forever and can be replayed at will. With the
    timestamp inside the MAC, a receiver rejects anything whose
    ``X-Stingray-Timestamp`` is outside its tolerance window, and an attacker
    cannot re-stamp it without the secret.

    Receivers must compare with a constant-time equality check
    (``hmac.compare_digest``), not ``==``.
    """
    message = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def delivery_uuid(delivery_id: int) -> str:
    """The stable ``X-Stingray-Delivery`` id for a delivery row."""
    return str(uuid.uuid5(DELIVERY_NAMESPACE, f"webhook-delivery:{delivery_id}"))


def build_body(delivery: WebhookDelivery) -> bytes:
    """Serialize the envelope that gets signed and sent.

    Serialization is pinned (sorted keys, no whitespace) because the bytes are
    the signed message: if the body were re-serialized differently between
    signing and sending, every signature would fail verification.
    """
    envelope: dict[str, Any] = {
        "id": delivery_uuid(delivery.id),
        "event": delivery.event_type,
        # The monotonic outbox id. This is the sequence consumers order on;
        # retries arrive out of order, this does not.
        "sequence": delivery.event_id,
        "ticket_id": delivery.ticket_id,
        "created_at": delivery.created_at.isoformat() if delivery.created_at else None,
        "data": delivery.payload or {},
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str).encode()


# --- Matching ----------------------------------------------------------------

def matches(webhook: Webhook, event_type: str, tags: list[str]) -> bool:
    """Whether ``webhook`` subscribes to this event. Pure, so it is testable.

    Both filters are empty-means-everything, and both must pass. Tag matching is
    ANY-of, which is what "follow one repo" needs (``["repo:foo"]``).
    """
    if not webhook.active:
        return False
    subscribed = webhook.event_types or []
    if subscribed and event_type not in subscribed:
        return False
    wanted = webhook.tag_filter or []
    if wanted and not (set(wanted) & set(tags or [])):
        return False
    return True


def _owner_can_see(db: Session, owner: Optional[User], ticket_id: Optional[int]) -> bool:
    """Whether the webhook's owner is allowed to know about this ticket.

    Sending is a *harder* boundary than the delivery log ``routers.webhooks``
    filters: the log merely displays a row, whereas a delivery ships the ticket
    title and tags to an address of the owner's choosing. So the same read
    boundary is applied here, at fan-out, and it is applied to the **owner** —
    the dispatcher runs with no caller.

    An event that names no ticket has nothing ticket-scoped to leak and passes.
    """
    if ticket_id is None:
        return True
    if owner is None:
        return False  # orphaned webhook: owner deleted
    from routers.tickets import _visible_tickets  # local: avoids an import cycle

    return (
        _visible_tickets(db, owner).filter(Ticket.id == ticket_id).first() is not None
    )


# --- Phase 1: claim ----------------------------------------------------------

def claim_batch(db: Session, limit: int = CLAIM_BATCH) -> list[int]:
    """Stamp ``claimed_at`` on up to ``limit`` unclaimed outbox rows and commit.

    Returns their ids rather than the ORM objects: the caller works on them in
    *later* sessions, and handing out instances from a session that is about to
    close is how you get detached-instance bugs.
    """
    rows = (
        db.query(Outbox)
        .filter(Outbox.claimed_at.is_(None))
        .order_by(Outbox.id.asc())
        .limit(limit)
        .all()
    )
    if not rows:
        return []
    now = utcnow()
    ids = []
    for row in rows:
        row.claimed_at = now
        ids.append(row.id)
    db.commit()
    return ids


# --- Phase 2: fan out --------------------------------------------------------

def fan_out(db: Session, event_id: int) -> int:
    """Create a pending ``WebhookDelivery`` per subscribed webhook. Commits.

    Returns how many deliveries were queued. Marking the outbox row delivered
    means "fanned out", not "the receivers got it" — the delivery rows own the
    rest of the lifecycle from here.
    """
    event = db.query(Outbox).filter(Outbox.id == event_id).first()
    if event is None:
        return 0
    if event.delivered_at is not None:
        return 0  # already fanned out; a re-claim must not double-queue

    payload = event.payload or {}
    tags = list(payload.get("ticket_tags") or [])
    queued = 0
    # Owner visibility is checked once per owner, not once per webhook, so a
    # user with several subscriptions costs one query rather than several.
    seen_owners: dict[int, bool] = {}

    for webhook in db.query(Webhook).filter(Webhook.active.is_(True)).all():
        if not matches(webhook, event.type, tags):
            continue
        allowed = seen_owners.get(webhook.user_id)
        if allowed is None:
            owner = db.query(User).filter(User.id == webhook.user_id).first()
            allowed = _owner_can_see(db, owner, event.ticket_id)
            seen_owners[webhook.user_id] = allowed
        if not allowed:
            continue
        db.add(
            WebhookDelivery(
                webhook_id=webhook.id,
                event_id=event.id,
                event_type=event.type,
                ticket_id=event.ticket_id,
                payload=payload,
                attempt_count=0,
                next_attempt_at=utcnow(),  # due immediately
                state=DeliveryState.pending.value,
            )
        )
        queued += 1

    event.delivered_at = utcnow()
    db.commit()
    return queued


# --- Phase 3: deliver --------------------------------------------------------

def due_delivery_ids(db: Session, limit: int = DELIVER_BATCH) -> list[int]:
    """Ids of pending deliveries whose ``next_attempt_at`` has come round.

    A NULL ``next_attempt_at`` counts as due — that is how ``redeliver`` and any
    hand-inserted row behave without having to know the schedule.
    """
    now = utcnow()
    rows = (
        db.query(WebhookDelivery.id)
        .filter(
            WebhookDelivery.state == DeliveryState.pending.value,
            (WebhookDelivery.next_attempt_at.is_(None))
            | (WebhookDelivery.next_attempt_at <= now),
        )
        .order_by(WebhookDelivery.id.asc())
        .limit(limit)
        .all()
    )
    return [row[0] for row in rows]


def _backoff_for(attempt: int) -> int:
    """Seconds to wait after ``attempt`` (1-based) has failed."""
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]


def _disable_webhook(db: Session, webhook: Webhook) -> None:
    """Switch a persistently-failing webhook off and tell its owner.

    Routed through ``inbox.create_notification`` on purpose: the bell is where a
    user already looks for things that happened to their stuff, and going
    through the helper means the notification-preferences gate applies to this
    like everything else. It stages the row; the caller commits.
    """
    webhook.active = False
    log.warning(
        "dispatcher: auto-disabled webhook %s (%s) after %s consecutive failures",
        webhook.id, webhook.url, webhook.consecutive_failures,
    )
    inbox.create_notification(
        db,
        user_id=webhook.user_id,
        type="webhook_disabled",
        ticket=None,
        actor=None,
        title=f"Webhook “{webhook.name or webhook.url}” was disabled after repeated failures",
    )


def _record_failure(
    db: Session,
    webhook: Webhook,
    delivery: WebhookDelivery,
    *,
    status_code: Optional[int],
    snippet: str,
    error: str,
) -> None:
    """Apply one failed attempt to the delivery and its webhook. Caller commits."""
    delivery.status_code = status_code
    delivery.response_snippet = snippet
    delivery.error = error[:MAX_RESPONSE_SNIPPET]
    webhook.consecutive_failures = (webhook.consecutive_failures or 0) + 1

    if delivery.attempt_count >= MAX_ATTEMPTS:
        delivery.state = DeliveryState.failed.value
        delivery.next_attempt_at = None
    else:
        delivery.state = DeliveryState.pending.value
        delivery.next_attempt_at = utcnow() + timedelta(
            seconds=_backoff_for(delivery.attempt_count)
        )

    if webhook.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and webhook.active:
        _disable_webhook(db, webhook)


async def deliver_one(client: httpx.AsyncClient, delivery_id: int) -> None:
    """Make (at most) one HTTP attempt for a delivery and record the outcome.

    Opens and closes its own session around the network call in three steps —
    read + mark ``delivering`` and commit, send with **no transaction open**,
    then reopen to write the result. Nothing here holds SQLite's write lock
    while a socket is waiting.
    """
    # --- Step 1: claim the row, gather what the request needs, commit.
    with SessionLocal() as db:
        delivery = db.query(WebhookDelivery).filter(WebhookDelivery.id == delivery_id).first()
        if delivery is None or delivery.state != DeliveryState.pending.value:
            return
        webhook = db.query(Webhook).filter(Webhook.id == delivery.webhook_id).first()
        if webhook is None or not webhook.active:
            # The subscription went away or was switched off between fan-out and
            # now. The event was never sent, which is exactly `skipped`.
            delivery.state = DeliveryState.skipped.value
            delivery.next_attempt_at = None
            delivery.error = "webhook is inactive or deleted"
            db.commit()
            return

        # DNS is mutable, so the target is re-checked *now* rather than trusted
        # from creation time — a host that was public then may resolve to
        # 127.0.0.1 by the time we connect (see webhook_urls).
        try:
            url = validate_webhook_url(webhook.url)
        except ValueError as exc:
            delivery.state = DeliveryState.skipped.value
            delivery.next_attempt_at = None
            delivery.error = str(exc)[:MAX_RESPONSE_SNIPPET]
            db.commit()
            log.warning("dispatcher: skipped delivery %s: %s", delivery_id, exc)
            return

        delivery.state = DeliveryState.delivering.value
        delivery.attempt_count = (delivery.attempt_count or 0) + 1
        body = build_body(delivery)
        secret = webhook.secret
        event_type = delivery.event_type
        db.commit()

    timestamp = str(int(utcnow().timestamp()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Stingray-Webhooks/1.0",
        "X-Stingray-Event": event_type,
        "X-Stingray-Delivery": delivery_uuid(delivery_id),
        "X-Stingray-Timestamp": timestamp,
        "X-Stingray-Signature": sign(secret, timestamp, body),
    }

    # --- Step 2: the network call, with no transaction open.
    response = None
    error = ""
    try:
        response = await client.post(
            url, content=body, headers=headers, timeout=REQUEST_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 — any transport failure is a retry
        error = f"{type(exc).__name__}: {exc}"

    # --- Step 3: record the outcome in a fresh, short transaction.
    with SessionLocal() as db:
        delivery = db.query(WebhookDelivery).filter(WebhookDelivery.id == delivery_id).first()
        if delivery is None:
            return  # deleted underneath us (webhook removed); nothing to record
        webhook = db.query(Webhook).filter(Webhook.id == delivery.webhook_id).first()
        if webhook is None:
            return

        if response is not None and 200 <= response.status_code < 300:
            delivery.state = DeliveryState.succeeded.value
            delivery.status_code = response.status_code
            delivery.response_snippet = (response.text or "")[:MAX_RESPONSE_SNIPPET]
            delivery.error = ""
            delivery.next_attempt_at = None
            webhook.consecutive_failures = 0
        elif response is not None:
            _record_failure(
                db, webhook, delivery,
                status_code=response.status_code,
                snippet=(response.text or "")[:MAX_RESPONSE_SNIPPET],
                error=f"HTTP {response.status_code}",
            )
        else:
            _record_failure(
                db, webhook, delivery, status_code=None, snippet="", error=error,
            )
        db.commit()


# --- The loop ----------------------------------------------------------------

async def drain_once(client: httpx.AsyncClient) -> int:
    """One full pass: claim → fan out → deliver everything due. Returns sends.

    Factored out of the loop so tests can drive a single deterministic pass
    instead of racing a background task.
    """
    with SessionLocal() as db:
        event_ids = claim_batch(db)
    for event_id in event_ids:
        with SessionLocal() as db:
            fan_out(db, event_id)

    with SessionLocal() as db:
        delivery_ids = due_delivery_ids(db)
    if not delivery_ids:
        return 0

    # Concurrent, bounded. One receiver hanging until REQUEST_TIMEOUT delays
    # only itself; the healthy ones in the same pass are already in flight.
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DELIVERIES)

    async def _guarded(delivery_id: int) -> None:
        async with semaphore:
            try:
                await deliver_one(client, delivery_id)
            except Exception:  # noqa: BLE001
                # One delivery blowing up must not cancel its siblings in the
                # gather, nor take the loop down.
                log.exception("dispatcher: delivery %s raised", delivery_id)

    await asyncio.gather(*(_guarded(i) for i in delivery_ids))
    return len(delivery_ids)


async def run_dispatcher() -> None:
    """The background task started from the FastAPI lifespan.

    Runs until cancelled. Every pass is wrapped: an unexpected error is logged
    and the loop sleeps and tries again, because a dispatcher that dies on one
    malformed row stops delivering everything.
    """
    log.info("dispatcher: started (interval=%ss)", DRAIN_INTERVAL)
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            while True:
                try:
                    await drain_once(client)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("dispatcher: drain pass failed")
                await asyncio.sleep(DRAIN_INTERVAL)
    except asyncio.CancelledError:
        log.info("dispatcher: stopped")
        raise
