"""Webhook subscriptions: CRUD plus the per-webhook delivery log.

Three boundaries hold this router up, and each is enforced in exactly one place:

1. **Owner scoping.** A webhook is looked up by ``(id, user_id)``, never by id
   alone (``_own_webhook_or_404``), and a webhook belonging to someone else is a
   404, not a 403 — a distinct 403 would confirm the id exists, which is all an
   enumerator wants. Admins may reach any webhook.
2. **The secret is never echoed.** ``WebhookOut`` has no ``secret`` field, so no
   read path can leak it however it is composed; the plaintext appears only in
   the create and rotate responses.
3. **Visibility of deliveries.** A delivery carries a ticket id, so listing the
   log is a ticket read — filtered against the *webhook owner's* visibility
   (``_visible_delivery_query``). Without that, a member could subscribe to
   ``ticket.created`` and read the titles of tickets they cannot open.

Delivery execution (the HTTP call, retries, HMAC signing with ``secret``) ships
separately; this router only stores subscriptions and re-arms rows.
"""
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from auth import get_current_user, is_admin
from database import get_db
from models import DeliveryState, Ticket, User, Webhook, WebhookDelivery, utcnow
from routers.tickets import _visible_tickets
from schemas import (
    MAX_WEBHOOKS_PER_USER,
    PaginatedDeliveries,
    WebhookCreate,
    WebhookCreated,
    WebhookDeliveryOut,
    WebhookOut,
    WebhookSecretRotated,
    WebhookUpdate,
)
from webhook_urls import validate_webhook_url

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SECRET_BYTES = 32
SECRET_PREFIX_LENGTH = 8


def _own_webhook_or_404(webhook_id: int, db: Session, user: User) -> Webhook:
    """The caller's webhook by id (any webhook, for an admin), else 404."""
    query = db.query(Webhook).filter(Webhook.id == webhook_id)
    if not is_admin(user):
        query = query.filter(Webhook.user_id == user.id)
    webhook = query.first()
    if not webhook:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
    return webhook


def _new_secret() -> str:
    return secrets.token_urlsafe(SECRET_BYTES)


def _visible_delivery_query(db: Session, webhook: Webhook):
    """Deliveries of ``webhook`` that its **owner** is allowed to see.

    The filter is deliberately keyed on the owner, not the caller: an admin
    reading a member's log must still see only what that member could, or the
    log becomes a way to learn what the member's webhook harvested. (It also
    means the admin's own view of their own webhook is unrestricted, since
    ``_visible_tickets`` does not filter for admins.)

    A row with no ``ticket_id`` (an event that names no ticket) carries nothing
    ticket-scoped to leak, so it always passes.
    """
    owner = db.query(User).filter(User.id == webhook.user_id).first()
    query = db.query(WebhookDelivery).filter(WebhookDelivery.webhook_id == webhook.id)
    if owner is None:
        # Orphaned webhook (owner deleted): show only the ticket-less rows.
        return query.filter(WebhookDelivery.ticket_id.is_(None))
    visible_ids = _visible_tickets(db, owner).with_entities(Ticket.id)
    return query.filter(
        (WebhookDelivery.ticket_id.is_(None))
        | (WebhookDelivery.ticket_id.in_(visible_ids))
    )


@router.get("", response_model=list[WebhookOut])
def list_webhooks(
    user_id: Optional[int] = Query(None, description="Admin only: scope to another user"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Webhook)
    if is_admin(user):
        if user_id is not None:
            query = query.filter(Webhook.user_id == user_id)
    else:
        # A non-admin's ?user_id= is ignored rather than honored — scoping to
        # someone else must not be expressible.
        query = query.filter(Webhook.user_id == user.id)
    return query.order_by(Webhook.created_at.desc()).all()


@router.post("", response_model=WebhookCreated, status_code=status.HTTP_201_CREATED)
def create_webhook(
    payload: WebhookCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a webhook. The response is the only time its secret is shown."""
    count = db.query(Webhook).filter(Webhook.user_id == user.id).count()
    if count >= MAX_WEBHOOKS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Too many webhooks (max {MAX_WEBHOOKS_PER_USER}); delete one first",
        )

    secret = _new_secret()
    webhook = Webhook(
        user_id=user.id,
        name=payload.name,
        url=payload.url,  # already SSRF-validated by the schema
        event_types=[e.value for e in payload.event_types],
        tag_filter=payload.tag_filter,
        secret=secret,
        secret_prefix=secret[:SECRET_PREFIX_LENGTH],
        active=payload.active,
    )
    db.add(webhook)
    db.commit()
    db.refresh(webhook)
    return webhook


@router.get("/{webhook_id}", response_model=WebhookOut)
def get_webhook(
    webhook_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _own_webhook_or_404(webhook_id, db, user)


@router.patch("/{webhook_id}", response_model=WebhookOut)
def update_webhook(
    webhook_id: int,
    payload: WebhookUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    webhook = _own_webhook_or_404(webhook_id, db, user)
    if payload.name is not None:
        webhook.name = payload.name
    if payload.url is not None:
        # Re-checked here as well as in the schema: DNS moves, and this is the
        # moment the stored target changes.
        webhook.url = validate_webhook_url(payload.url)
    if payload.event_types is not None:
        webhook.event_types = [e.value for e in payload.event_types]
    if payload.tag_filter is not None:
        webhook.tag_filter = payload.tag_filter
    if payload.active is not None:
        if payload.active and not webhook.active:
            # Re-enabling is the operator saying "I fixed it" — start the
            # failure count over so an old streak can't immediately re-trip an
            # auto-disable policy.
            webhook.consecutive_failures = 0
        webhook.active = payload.active
    db.commit()
    db.refresh(webhook)
    return webhook


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook(
    webhook_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    webhook = _own_webhook_or_404(webhook_id, db, user)
    db.delete(webhook)  # cascades the delivery log
    db.commit()
    return None


@router.post("/{webhook_id}/rotate-secret", response_model=WebhookSecretRotated)
def rotate_webhook_secret(
    webhook_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Issue a new signing secret, shown exactly once (the old one stops signing)."""
    webhook = _own_webhook_or_404(webhook_id, db, user)
    secret = _new_secret()
    webhook.secret = secret
    webhook.secret_prefix = secret[:SECRET_PREFIX_LENGTH]
    db.commit()
    return WebhookSecretRotated(
        id=webhook.id, secret=secret, secret_prefix=webhook.secret_prefix
    )


@router.get("/{webhook_id}/deliveries", response_model=PaginatedDeliveries)
def list_deliveries(
    webhook_id: int,
    state: Optional[str] = Query(None, description="Filter by DeliveryState value"),
    ticket_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    webhook = _own_webhook_or_404(webhook_id, db, user)
    query = _visible_delivery_query(db, webhook)
    if state is not None:
        if state not in {s.value for s in DeliveryState}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown delivery state {state!r}",
            )
        query = query.filter(WebhookDelivery.state == state)
    if ticket_id is not None:
        query = query.filter(WebhookDelivery.ticket_id == ticket_id)

    total = query.count()
    items = (
        query.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return PaginatedDeliveries(items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/{webhook_id}/deliveries/{delivery_id}/redeliver",
    response_model=WebhookDeliveryOut,
)
def redeliver(
    webhook_id: int,
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Re-arm a delivery for another attempt.

    This does not send anything — the delivery worker owns the socket. It resets
    the outcome fields and queues the row; ``attempt_count`` is left intact
    because it is history, and the log's value is that history.
    """
    webhook = _own_webhook_or_404(webhook_id, db, user)
    delivery = (
        _visible_delivery_query(db, webhook)
        .filter(WebhookDelivery.id == delivery_id)
        .first()
    )
    if not delivery:
        # Also the "exists but names a ticket you can't see" case: 404, same
        # reasoning as _own_webhook_or_404.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")

    # DNS may have moved since the webhook was created, so the target is
    # re-validated before anything is queued against it.
    try:
        validate_webhook_url(webhook.url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    delivery.state = DeliveryState.pending.value
    delivery.next_attempt_at = utcnow()
    delivery.status_code = None
    delivery.error = ""
    db.commit()
    db.refresh(delivery)
    return delivery
