"""Ticket routes: list/filter, create, retrieve, update, delete."""
import secrets
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import and_, case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from activity import record_activity
from auth import (
    can_modify_ticket,
    can_view_ticket,
    get_api_key,
    get_current_user,
    is_admin,
)
from control_tags import (
    RESERVED_EXACT,
    RESERVED_PREFIXES,
    can_set_tag,
    is_reserved_tag,
    unauthorized_tags,
)
from database import get_db
from events import emit
from inbox import create_notification
from models import (
    PRIORITY_ORDER,
    Activity,
    AgentRun,
    AgentRunStatus,
    Ticket,
    TicketLease,
    TicketPriority,
    TicketStatus,
    TicketType,
    User,
    utcnow,
)
from notifications import notify_assignment, notify_new_ticket_admins
from routers.security_settings import get_security_settings
from schemas import (
    MAX_TAGS,
    ActivityOut,
    AgentRunCreate,
    AgentRunOut,
    AgentRunTotals,
    ClaimRequest,
    CostRollup,
    CostRollupChild,
    LeaseExtend,
    LeaseOut,
    LeaseRelease,
    PaginatedTickets,
    TagFacet,
    TagFacets,
    TicketCreate,
    TicketOut,
    TicketUpdate,
)
from ticket_queries import tag_clause, visible_tickets

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _get_ticket_or_404(ticket_id: int, db: Session) -> Ticket:
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


def _reserved_tag_error(rejected: set[str]) -> str:
    """Explain which tags were refused and what would be needed to set them.

    Built from the control_tags constants rather than hardcoded, so it can't go
    stale the way the old fixed string did (it still named only four of the seven
    reserved forms long after `parent:`, `review-by:` and `delegate` were added).
    """
    reserved = ", ".join(sorted(f"{p}*" for p in RESERVED_PREFIXES) + sorted(RESERVED_EXACT))
    msg = (
        f"Reserved tags ({reserved}) cannot be set; they are managed by the "
        f"automation. Refused: {', '.join(sorted(rejected))}."
    )
    if any(t.startswith("repo:") for t in rejected):
        msg += " An API key with the 'cli' scope may set repo: tags."
    return msg


def _authorize_tags(
    submitted: list[str], existing: list[str] | None, user: User, api_key
) -> list[str]:
    """Compute the tag set to store, enforcing per-tag authority.

    Submitting a tag this caller may not set is rejected outright. Reserved tags
    already on the ticket that the caller may *not* set are preserved, so a
    free-tag edit can never strip a control tag.

    Note what is deliberately *not* preserved: reserved tags the caller IS allowed
    to set are fully caller-controlled, so a `cli`-scoped key can change its own
    ticket's `repo:a` to `repo:b`. The old code preserved every reserved tag
    unconditionally, which would have made a scoped key able to add a repo tag but
    never correct one.
    """
    bad = unauthorized_tags(user, api_key, submitted)
    if bad:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=_reserved_tag_error(bad))
    pinned = [
        t for t in (existing or [])
        if is_reserved_tag(t) and not can_set_tag(user, api_key, t)
    ]
    return pinned + [t for t in submitted if t not in pinned]


# The read boundary and exact tag matching live in ``ticket_queries`` so the chat
# assistant's read-only tools start from the *same* definitions these routes do —
# one place to change if the boundary ever moves. Aliased to the private names the
# rest of this module already uses.
_visible_tickets = visible_tickets
_tag_clause = tag_clause


# `?sort=` value -> the column to order by. `priority` is a String column, so
# ordering by it directly is alphabetical and meaningless; PRIORITY_ORDER (in
# models, beside the enum) becomes a CASE that ranks critical..low.
_SORT_COLUMNS = {
    "created": Ticket.created_at,
    "updated": Ticket.updated_at,
    "title": Ticket.title,
    "due": Ticket.due_date,
    "priority": case(PRIORITY_ORDER, value=Ticket.priority, else_=len(PRIORITY_ORDER)),
}


def _order_by(sort: str, order: str):
    """Ordering terms for a sort/direction pair.

    Two wrinkles worth stating: `priority` reads best ascending (most urgent
    first), so its rank column is inverted relative to dates, where "desc" means
    newest first. And tickets with no due date sort last in *both* directions —
    an empty due date is "no deadline", not "the earliest deadline".
    """
    column = _SORT_COLUMNS[sort]
    descending = (order == "desc")
    if sort == "priority":
        descending = not descending
    terms = []
    if sort == "due":
        terms.append(Ticket.due_date.is_(None).asc())
    terms.append(column.desc() if descending else column.asc())
    # Stable tiebreak so paging can't repeat or skip a row when the sort key ties.
    terms.append(Ticket.id.desc())
    return terms


@router.get("/tags", response_model=TagFacets)
def list_ticket_tags(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    archived: Optional[bool] = Query(default=None),
):
    """Every tag on a ticket this caller can see, with a usage count.

    Feeds the dashboard's tag picker. Tags live in a JSON column with no portable
    SQL unnest, so the counting happens in Python over just that one column —
    fine at this scale, and it keeps the visibility rules in one place
    (`_visible_tickets`) instead of duplicating them into raw SQL.

    Declared before `/{ticket_id}` so the literal path wins the route match.
    """
    query = _visible_tickets(db, user).with_entities(Ticket.tags)
    if archived is None:
        query = query.filter(Ticket.archived == False)  # noqa: E712
    else:
        query = query.filter(Ticket.archived == archived)

    counts: Counter[str] = Counter()
    for (tags,) in query.all():
        counts.update(t for t in (tags or []) if isinstance(t, str))

    # Most-used first, then alphabetically so the order is stable between calls.
    items = [
        TagFacet(tag=tag, count=count)
        for tag, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return TagFacets(items=items)


@router.get("", response_model=PaginatedTickets)
def list_tickets(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    status: Optional[TicketStatus] = Query(default=None),
    type: Optional[TicketType] = Query(default=None),
    assigned_to: Optional[int] = Query(default=None),
    created_by: Optional[int] = Query(default=None),
    priority: Optional[TicketPriority] = Query(default=None),
    # Repeatable: ?tag=a&tag=b. A single ?tag= still arrives as a one-element
    # list, so pre-existing callers (the CLI, api_guide.md) are unaffected.
    tag: Optional[List[str]] = Query(default=None),
    tag_match: Literal["all", "any"] = Query(default="all"),
    q: Optional[str] = Query(default=None),
    archived: Optional[bool] = Query(default=None),
    sort: Literal["created", "updated", "priority", "due", "title"] = Query(default="created"),
    order: Literal["asc", "desc"] = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    query = _visible_tickets(db, user)
    if status is not None:
        query = query.filter(Ticket.status == status.value)
    if type is not None:
        query = query.filter(Ticket.type == type.value)
    if assigned_to is not None:
        query = query.filter(Ticket.assigned_to == assigned_to)
    if created_by is not None:
        query = query.filter(Ticket.created_by == created_by)
    if priority is not None:
        query = query.filter(Ticket.priority == priority.value)
    # Archived tickets are hidden by default; pass archived=true for the archive view.
    if archived is None:
        query = query.filter(Ticket.archived == False)  # noqa: E712
    else:
        query = query.filter(Ticket.archived == archived)
    # Blank values survive the frontend's param building, so ignore them rather
    # than filtering for a tag that is the empty string (which matches nothing).
    tags = [t.strip() for t in (tag or []) if t and t.strip()]
    if tags:
        clauses = [_tag_clause(t) for t in tags]
        query = query.filter(and_(*clauses) if tag_match == "all" else or_(*clauses))
    # Free-text search over title/description; ignore an empty/whitespace-only term.
    if q is not None and q.strip():
        term = q.strip()
        query = query.filter(
            or_(Ticket.title.ilike(f"%{term}%"), Ticket.description.ilike(f"%{term}%"))
        )

    total = query.count()
    items = (
        query.order_by(*_order_by(sort, order)).offset(offset).limit(limit).all()
    )
    return PaginatedTickets(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    api_key=Depends(get_api_key),
):
    assignee = None
    if payload.assigned_to is not None:
        assignee = db.query(User).filter(User.id == payload.assigned_to).first()
        if not assignee:
            raise HTTPException(status_code=400, detail="assigned_to user does not exist")

    # code_blocks only carry meaning for code_review tickets.
    code_blocks = (
        [cb.model_dump() for cb in payload.code_blocks]
        if payload.type == TicketType.code_review
        else []
    )

    # Reserved control tags need a trusted identity (admin / resolver bot) or a
    # key scoped for that tag; everyone else may set free tags only. (No existing
    # tags on create.)
    tags = payload.tags
    if tags:
        tags = _authorize_tags(tags, None, user, api_key)

    ticket = Ticket(
        type=payload.type.value,
        title=payload.title,
        description=payload.description,
        status=payload.status.value,
        priority=payload.priority.value,
        created_by=user.id,
        assigned_to=payload.assigned_to,
        due_date=payload.due_date,
        code_blocks=code_blocks,
        tags=tags,
    )
    db.add(ticket)
    db.flush()  # assign ticket.id for the activity rows

    record_activity(db, ticket.id, user.id, "created")
    emit(db, type="ticket.created", ticket=ticket, actor=user)
    if assignee is not None:
        record_activity(
            db, ticket.id, user.id, "assigned",
            {"to": assignee.id, "name": assignee.display_name},
        )
        create_notification(
            db, user_id=assignee.id, type="assigned", ticket=ticket, actor=user,
        )
        emit(db, type="ticket.assigned", ticket=ticket, actor=user,
             delta={"to": assignee.id, "name": assignee.display_name})
    db.commit()
    db.refresh(ticket)

    # Notify admins of the new ticket, and the assignee (if any, and not the author).
    notify_new_ticket_admins(background, db, ticket, user)
    if assignee is not None:
        notify_assignment(background, db, ticket, assignee, user)
    return ticket


@router.get("/{ticket_id}", response_model=TicketOut)
def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(ticket_id, db)
    # Return 404 (not 403) so non-members can't confirm a ticket ID exists.
    if not can_view_ticket(user, ticket):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


@router.patch("/{ticket_id}", response_model=TicketOut)
def update_ticket(
    ticket_id: int,
    payload: TicketUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    api_key=Depends(get_api_key),
):
    ticket = _get_ticket_or_404(ticket_id, db)
    if not can_modify_ticket(user, ticket):
        raise HTTPException(status_code=403, detail="Not permitted to modify this ticket")

    data = payload.model_dump(exclude_unset=True)

    new_assignee = None
    if "assigned_to" in data and data["assigned_to"] is not None:
        new_assignee = db.query(User).filter(User.id == data["assigned_to"]).first()
        if not new_assignee:
            raise HTTPException(status_code=400, detail="assigned_to user does not exist")

    # Reserved control tags drive the resolver's automation, so setting one needs
    # a trusted identity or a key scoped for it. For everyone else a tags edit
    # controls free tags only, and the ticket's existing control tags are
    # preserved — see _authorize_tags.
    if "tags" in data and data["tags"] is not None:
        data["tags"] = _authorize_tags(data["tags"], ticket.tags, user, api_key)

    # Snapshot the fields we audit, before mutating.
    old_status = ticket.status
    old_priority = ticket.priority
    old_assigned_to = ticket.assigned_to
    old_tags = list(ticket.tags or [])

    for field in ("title", "description", "status", "priority", "assigned_to", "due_date", "tags"):
        if field in data:
            value = data[field]
            # Enum fields arrive as Enum instances; store their value.
            if field in ("status", "priority") and value is not None:
                value = value.value if hasattr(value, "value") else value
            setattr(ticket, field, value)

    if "code_blocks" in data and data["code_blocks"] is not None:
        if ticket.type != TicketType.code_review.value:
            raise HTTPException(status_code=400, detail="code_blocks only allowed on code_review tickets")
        ticket.code_blocks = [cb.model_dump() for cb in data["code_blocks"]]

    # Record activity for the audited fields that actually changed.
    if "status" in data and ticket.status != old_status:
        record_activity(db, ticket.id, user.id, "status_changed",
                        {"from": old_status, "to": ticket.status})
        emit(db, type="ticket.status_changed", ticket=ticket, actor=user,
             delta={"from": old_status, "to": ticket.status})
    if "priority" in data and ticket.priority != old_priority:
        record_activity(db, ticket.id, user.id, "priority_changed",
                        {"from": old_priority, "to": ticket.priority})
    if "assigned_to" in data and ticket.assigned_to != old_assigned_to:
        if ticket.assigned_to is None:
            record_activity(db, ticket.id, user.id, "unassigned")
        else:
            record_activity(db, ticket.id, user.id, "assigned",
                            {"to": new_assignee.id, "name": new_assignee.display_name})
            create_notification(
                db, user_id=new_assignee.id, type="assigned", ticket=ticket, actor=user,
            )
            emit(db, type="ticket.assigned", ticket=ticket, actor=user,
                 delta={"to": new_assignee.id, "name": new_assignee.display_name})
    if "tags" in data and data["tags"] is not None:
        new_tags = list(ticket.tags or [])
        if new_tags != old_tags:
            added = [t for t in new_tags if t not in old_tags]
            removed = [t for t in old_tags if t not in new_tags]
            record_activity(db, ticket.id, user.id, "tags_changed",
                            {"added": added, "removed": removed})
            emit(db, type="ticket.tagged", ticket=ticket, actor=user,
                 delta={"added": added, "removed": removed})

    ticket.updated_at = utcnow()
    db.commit()
    db.refresh(ticket)

    # Notify a newly-assigned (non-self) user.
    if new_assignee is not None and old_assigned_to != new_assignee.id:
        notify_assignment(background, db, ticket, new_assignee, user)
    return ticket


@router.delete("/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(ticket_id, db)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    db.delete(ticket)
    db.commit()
    return None


@router.get("/{ticket_id}/activity", response_model=list[ActivityOut])
def list_activity(
    ticket_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    _get_ticket_or_404(ticket_id, db)
    return (
        db.query(Activity)
        .filter(Activity.ticket_id == ticket_id)
        .order_by(Activity.created_at.asc(), Activity.id.asc())
        .all()
    )


# --- Leases ------------------------------------------------------------------
# An explicit, expiring claim on a ticket. See models.TicketLease for why this
# exists at all; what lives here is the policy around it.
#
# Expiry is *lazy*: stale rows are swept at the top of a claim rather than by a
# background task. A lease only matters at the moment someone tries to take one,
# so the sweep is exactly as timely as it needs to be and the app keeps its "no
# extra moving parts" shape. Reads (`_live_lease`) additionally ignore rows whose
# `expires_at` has passed, so an unswept row is never mistaken for a live claim.

# Mirror of the claim in the ticket's tag list. Not authoritative — it exists so
# a human reading the ticket, and the resolver's own pre-lease tag logic, can see
# that a worker holds it. `resolver:` tags are deliberately not reserved
# server-side, so this needs no special authority to write.
CLAIM_MIRROR_TAG = "resolver:claimed"


def _naive_utc(dt: datetime) -> datetime:
    """DB datetimes come back naive-UTC while ``utcnow()`` is aware; normalize
    before comparing the two in Python (the same dance ``auth._expired`` does)."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _expire_stale_leases(db: Session) -> None:
    """Drop every lease whose TTL has run out.

    ``<=`` matches ``_live_lease``'s boundary exactly. If the two disagreed, a
    lease expiring on this very second would read as dead but survive the sweep,
    and the claim that followed would trip the unique constraint instead of
    succeeding.
    """
    deleted = (
        db.query(TicketLease)
        .filter(TicketLease.expires_at <= utcnow())
        .delete(synchronize_session=False)
    )
    if deleted:
        db.commit()


def _live_lease(db: Session, ticket_id: int) -> TicketLease | None:
    """This ticket's lease if one is held and still within its TTL.

    Checks the expiry in Python rather than in SQL so a row the lazy sweep has
    not reached yet still reads as expired — the caller must never see a lapsed
    claim as live, whatever order requests happen to arrive in.
    """
    lease = db.query(TicketLease).filter(TicketLease.ticket_id == ticket_id).first()
    if lease is None:
        return None
    if _naive_utc(lease.expires_at) <= _naive_utc(utcnow()):
        return None
    return lease


def _set_claim_mirror(ticket: Ticket, held: bool) -> None:
    """Add/remove the advisory claim tag. JSON columns need reassignment for
    SQLAlchemy to notice the change, so build a new list either way."""
    tags = [t for t in (ticket.tags or []) if t != CLAIM_MIRROR_TAG]
    if held and len(tags) < MAX_TAGS:
        tags.append(CLAIM_MIRROR_TAG)
    ticket.tags = tags


def _lease_ticket_or_404(ticket_id: int, db: Session, user: User) -> Ticket:
    """The ticket, if this caller may work it.

    Claiming is gated on the same authority as modifying: a lease is a promise to
    write results back, so being able to take one without being able to act on
    the ticket would be worse than useless — it would let any member freeze the
    queue's tickets out from under their assignees. 404 rather than 403 for a
    ticket the caller cannot see, matching ``get_ticket``.
    """
    ticket = _get_ticket_or_404(ticket_id, db)
    if not can_modify_ticket(user, ticket):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


def _held_lease_or_error(ticket_id: int, token: str, db: Session) -> TicketLease:
    """The live lease on ``ticket_id``, proving the caller presented its token.

    404 when there is nothing (or nothing live) to act on — which is what a
    worker whose lease already expired sees, and why release/extend on a lapsed
    claim is an ordinary answer rather than a server error. 403 when a lease
    exists but the token is wrong: that is a *different* worker's claim, and
    saying so is the point.
    """
    lease = _live_lease(db, ticket_id)
    if lease is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No active lease on this ticket")
    if not secrets.compare_digest(lease.token, token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Lease token does not match the current holder")
    return lease


@router.post("/{ticket_id}/claim", response_model=LeaseOut)
def claim_ticket(
    ticket_id: int,
    payload: ClaimRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Take an exclusive, expiring claim on a ticket, or 409 if someone holds it.

    The unique constraint on ``ticket_leases.ticket_id`` — not the SELECT below —
    is what makes this safe: two sweeps that both read "unclaimed" still cannot
    both insert, and the loser's ``IntegrityError`` becomes the same 409 the
    early check would have produced. The check is kept because it answers without
    burning a failed write in the common case.
    """
    payload = payload or ClaimRequest()
    _expire_stale_leases(db)
    ticket = _lease_ticket_or_404(ticket_id, db, user)

    existing = _live_lease(db, ticket_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ticket is already claimed by user {existing.worker_id}",
        )

    # The schema's Field(ge=MIN_LEASE_TTL, le=MAX_LEASE_TTL) is the absolute
    # hard rail (rejects nonsense outright); this is the admin-editable policy
    # window *inside* it — an admin can only tighten the band, never widen past
    # the hard rail (SecuritySettingsValues enforces that at write time).
    security_settings = get_security_settings(db)
    ttl_seconds = (
        payload.ttl_seconds
        if "ttl_seconds" in payload.model_fields_set
        else security_settings.default_lease_ttl
    )
    if not (security_settings.min_lease_ttl <= ttl_seconds <= security_settings.max_lease_ttl):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"ttl_seconds must be between {security_settings.min_lease_ttl} and "
                f"{security_settings.max_lease_ttl}"
            ),
        )

    lease = TicketLease(
        ticket_id=ticket.id,
        worker_id=user.id,
        token=secrets.token_urlsafe(32),
        expires_at=utcnow() + timedelta(seconds=ttl_seconds),
    )
    db.add(lease)
    try:
        db.flush()
    except IntegrityError:
        # Lost the race to another claimant between the SELECT and the INSERT.
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Ticket is already claimed")
    _set_claim_mirror(ticket, True)
    db.commit()
    db.refresh(lease)
    return lease


@router.post("/{ticket_id}/release", status_code=status.HTTP_204_NO_CONTENT)
def release_ticket(
    ticket_id: int,
    payload: LeaseRelease,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Give up a claim early, so the ticket is workable again without waiting out
    its TTL. Idempotent-ish: releasing a lease that already expired is a 404, not
    an error the caller has to handle specially."""
    ticket = _lease_ticket_or_404(ticket_id, db, user)
    lease = _held_lease_or_error(ticket_id, payload.token, db)
    db.delete(lease)
    _set_claim_mirror(ticket, False)
    db.commit()
    return None


@router.post("/{ticket_id}/lease/extend", response_model=LeaseOut)
def extend_lease(
    ticket_id: int,
    payload: LeaseExtend,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Push a live claim's expiry out — the heartbeat a long-running worker sends.

    Deliberately cannot resurrect an expired lease: once the TTL lapses the
    ticket is back in the queue and may already have been re-claimed, so a worker
    that slept through its own heartbeat must go round again via ``/claim``.
    """
    _lease_ticket_or_404(ticket_id, db, user)
    lease = _held_lease_or_error(ticket_id, payload.token, db)
    lease.expires_at = utcnow() + timedelta(seconds=payload.ttl_seconds)
    db.commit()
    db.refresh(lease)
    return lease


@router.post(
    "/{ticket_id}/agent-runs",
    response_model=AgentRunOut,
    status_code=status.HTTP_201_CREATED,
)
def create_agent_run(
    ticket_id: int,
    payload: AgentRunCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Record one resolver phase (its model, token usage, cost, status).

    Posted by the resolver (authenticating as the claude-bot via X-API-Key) as it
    finishes each phase. We gate on `can_modify_ticket`: during a run the bot is
    the ticket's assignee, so it passes; a random member who can't touch the
    ticket can't forge runs against it. (Admins also pass, which is fine for
    backfills/manual entry.)

    A caller holding a lease may additionally send its ``lease_token``, which is
    refused with 409 once the lease has lapsed. That is the write-side half of
    the claim: without it a worker that stalled past its TTL — and whose ticket
    another worker has since picked up — would still be able to post results for
    a run nobody is waiting for. The field is optional, so pre-lease callers are
    unaffected.
    """
    ticket = _get_ticket_or_404(ticket_id, db)
    if not can_modify_ticket(user, ticket):
        raise HTTPException(status_code=403, detail="Not permitted to modify this ticket")
    if payload.lease_token is not None:
        lease = _live_lease(db, ticket_id)
        if lease is None or not secrets.compare_digest(lease.token, payload.lease_token):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Lease has expired or is held by another worker",
            )

    run = AgentRun(
        ticket_id=ticket.id,
        agent=payload.agent,
        phase=payload.phase,
        model=payload.model,
        input_tokens=payload.input_tokens,
        output_tokens=payload.output_tokens,
        cache_read_tokens=payload.cache_read_tokens,
        cache_write_tokens=payload.cache_write_tokens,
        cost_usd=payload.cost_usd,
        status=payload.status,
        # Only a failed run's tail is kept. A successful transcript is bulk
        # nobody reads, and the point of storing any of it is to answer "why did
        # this fail?" — so a tail arriving on a succeeded run is dropped here
        # rather than trusted, since the sender is an unattended bot.
        log_tail=payload.log_tail if payload.status == AgentRunStatus.failed.value else "",
        started_at=payload.started_at,
        # Default the completion time server-side if the caller omits it.
        finished_at=payload.finished_at or utcnow(),
    )
    db.add(run)
    emit(db, type="agent_run.finished", ticket=ticket, actor=user,
         delta={"agent": run.agent, "phase": run.phase, "status": run.status})
    db.commit()
    db.refresh(run)
    return run


@router.get("/{ticket_id}/agent-runs", response_model=list[AgentRunOut])
def list_agent_runs(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(ticket_id, db)
    # 404 (not 403) so non-members can't probe ticket existence — same as get_ticket.
    if not can_view_ticket(user, ticket):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return (
        db.query(AgentRun)
        .filter(AgentRun.ticket_id == ticket_id)
        .order_by(AgentRun.started_at.asc().nullslast(), AgentRun.id.asc())
        .all()
    )


def _agent_run_totals(db: Session, ticket_ids: list[int]) -> AgentRunTotals:
    """Sum cost + token usage across every agent run on the given tickets."""
    if not ticket_ids:
        return AgentRunTotals()
    row = (
        db.query(
            func.coalesce(func.sum(AgentRun.cost_usd), 0.0),
            func.coalesce(func.sum(AgentRun.input_tokens), 0),
            func.coalesce(func.sum(AgentRun.output_tokens), 0),
            func.coalesce(func.sum(AgentRun.cache_read_tokens), 0),
            func.coalesce(func.sum(AgentRun.cache_write_tokens), 0),
            func.count(AgentRun.id),
        )
        .filter(AgentRun.ticket_id.in_(ticket_ids))
        .one()
    )
    return AgentRunTotals(
        cost_usd=float(row[0]),
        input_tokens=int(row[1]),
        output_tokens=int(row[2]),
        cache_read_tokens=int(row[3]),
        cache_write_tokens=int(row[4]),
        run_count=int(row[5]),
    )


@router.get("/{ticket_id}/cost-rollup", response_model=CostRollup)
def cost_rollup(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """This ticket's own agent-run cost plus the cost of every delegated child.

    Children are sub-tasks filed by the resolver carrying a ``parent:<id>`` tag;
    we sum their runs too so a delegating ticket shows the whole fan-out's spend.
    """
    ticket = _get_ticket_or_404(ticket_id, db)
    # 404 (not 403) so non-members can't probe ticket existence — same as get_ticket.
    if not can_view_ticket(user, ticket):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    own = _agent_run_totals(db, [ticket_id])
    # Same JSON-array substring match the list endpoint uses for tag filtering.
    children = (
        db.query(Ticket)
        .filter(Ticket.tags.like(f'%"parent:{ticket_id}"%'))
        .order_by(Ticket.id.asc())
        .all()
    )
    child_out = [
        CostRollupChild(
            ticket_id=c.id,
            title=c.title,
            totals=_agent_run_totals(db, [c.id]),
        )
        for c in children
    ]
    total = _agent_run_totals(db, [ticket_id] + [c.id for c in children])
    return CostRollup(ticket_id=ticket_id, own=own, children=child_out, total=total)


@router.post("/{ticket_id}/archive", response_model=TicketOut)
def archive_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(ticket_id, db)
    if not can_modify_ticket(user, ticket):
        raise HTTPException(status_code=403, detail="Not permitted to modify this ticket")
    if ticket.status != TicketStatus.closed.value:
        raise HTTPException(status_code=400, detail="Only closed tickets can be archived")
    ticket.archived = True
    ticket.updated_at = utcnow()
    db.commit()
    db.refresh(ticket)
    return ticket


@router.post("/{ticket_id}/unarchive", response_model=TicketOut)
def unarchive_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(ticket_id, db)
    if not can_modify_ticket(user, ticket):
        raise HTTPException(status_code=403, detail="Not permitted to modify this ticket")
    ticket.archived = False
    ticket.updated_at = utcnow()
    db.commit()
    db.refresh(ticket)
    return ticket
