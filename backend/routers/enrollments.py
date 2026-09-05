"""Station enrolment: give a host one bot's credentials without an admin key.

Creating a resolver bot and minting its key are admin operations, but the host
that will *run* the bot is also the host executing untrusted agent output —
which makes it the last place an admin credential should live. So the admin
mints a short-lived, single-use token in the browser and the workstation
redeems it for exactly one bot.

Two asymmetries are deliberate:

*Minting needs ``require_recent_admin``*, which cannot be satisfied by an API
key at all — it reads the session cookie's age. That is the point: the whole
feature exists so no program holds the authority to create bots, and a gate a
program cannot pass is the only way to mean it.

*Redeeming needs no authentication*, because the station has nothing yet. The
token is the credential, so it is treated like one: high entropy, hashed at
rest, single use, short lived, rate limited per IP, and every failure answers
with the same message so the endpoint cannot be used to tell a real token from
an expired one.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import secrets

from auth import hash_api_key, require_admin, require_recent_admin
from database import get_db
from models import StationEnrollment, User, utcnow
from ratelimit import limiter
from schemas import (
    StationEnrollmentCreate,
    StationEnrollmentCreated,
    StationEnrollmentOut,
    StationEnrollmentRedeem,
    StationEnrollmentRedeemed,
)
from seed import create_resolver_bot

router = APIRouter(prefix="/station-enrollments", tags=["station-enrollments"])

# Per-IP budget for redeeming. A token is 32 bytes of entropy, so this is not
# what makes guessing infeasible — it bounds the noise an unauthenticated
# endpoint can be made to generate.
REDEEM_RATE_LIMIT = "10/minute;60/hour"

# One answer for every failure. Distinguishing "no such token" from "expired"
# or "already redeemed" would turn this endpoint into an oracle for which
# tokens ever existed.
_REJECTED = "invalid or expired enrolment token"


def _new_token() -> str:
    return "st_" + secrets.token_urlsafe(32)


def _out(row: StationEnrollment) -> StationEnrollmentOut:
    return StationEnrollmentOut(
        id=row.id,
        username=row.username,
        display_name=row.display_name or "",
        token_prefix=row.token_prefix,
        created_at=row.created_at,
        expires_at=row.expires_at,
        redeemed_at=row.redeemed_at,
        redeemed_user_id=row.redeemed_user_id,
        station=row.station or "",
    )


@router.post("", response_model=StationEnrollmentCreated,
             status_code=status.HTTP_201_CREATED)
def create_enrollment(
    payload: StationEnrollmentCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_recent_admin),
):
    """Mint a token for one named bot. The plaintext is returned exactly once.

    The bot is *not* created here. Creating it at mint time would leave an
    orphan user behind every token nobody redeems, and the roster would fill
    with identities that have never run anything.
    """
    from datetime import timedelta

    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")

    raw = _new_token()
    row = StationEnrollment(
        token_prefix=raw[:11],
        token_hash=hash_api_key(raw),
        username=payload.username,
        display_name=payload.display_name or payload.username,
        email=payload.email or "",
        created_by=admin.id,
        expires_at=utcnow() + timedelta(seconds=payload.expires_in_seconds),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return StationEnrollmentCreated(
        id=row.id, username=row.username, token=raw, expires_at=row.expires_at,
    )


@router.get("", response_model=list[StationEnrollmentOut])
def list_enrollments(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Every enrolment, newest first. Never includes a token."""
    rows = (
        db.query(StationEnrollment)
        .order_by(StationEnrollment.created_at.desc())
        .all()
    )
    return [_out(r) for r in rows]


@router.delete("/{enrollment_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_enrollment(
    enrollment_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Withdraw a token that has not been used.

    A redeemed one is deliberately not deletable: the bot it created still
    exists, and the row is the only record of how that identity came to be.
    Revoking access to *that* means revoking the bot's API key, which is a
    different operation with a different blast radius.
    """
    row = db.query(StationEnrollment).filter(StationEnrollment.id == enrollment_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Enrolment not found")
    if row.redeemed_at is not None:
        raise HTTPException(
            status_code=409,
            detail="Already redeemed — revoke the bot's API key instead",
        )
    db.delete(row)
    db.commit()
    return None


@router.post("/redeem", response_model=StationEnrollmentRedeemed)
@limiter.limit(REDEEM_RATE_LIMIT)
def redeem_enrollment(
    request: Request,
    payload: StationEnrollmentRedeem,
    db: Session = Depends(get_db),
):
    """Exchange a token for one bot and its first API key. No auth: the token is it.

    ``request`` is unused by the body of this function but required by slowapi,
    which reads the client address off it.
    """
    row = (
        db.query(StationEnrollment)
        .filter(StationEnrollment.token_hash == hash_api_key(payload.token))
        .first()
    )
    now = utcnow()
    # Every rejection is the same 404. A token that never existed, one that
    # expired, and one already spent must be indistinguishable from outside.
    if row is None or row.redeemed_at is not None or _naive(row.expires_at) <= _naive(now):
        raise HTTPException(status_code=404, detail=_REJECTED)

    bot, raw_key = create_resolver_bot(
        db, row.username,
        display_name=row.display_name or row.username,
        email=row.email or None,
    )
    # Claim the token in the same transaction that creates the bot, so a
    # failure cannot leave a spent token with no identity behind it — or an
    # identity created twice by two racing redemptions.
    row.redeemed_at = now
    row.station = (payload.station or "")[:200]
    try:
        db.flush()
        row.redeemed_user_id = bot.id
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username already exists") from None
    db.refresh(bot)
    return StationEnrollmentRedeemed(
        user_id=bot.id, username=bot.username, api_key=raw_key,
    )


def _naive(value):
    """Compare stored (naive UTC) and generated timestamps on equal footing."""
    return value.replace(tzinfo=None) if getattr(value, "tzinfo", None) else value
