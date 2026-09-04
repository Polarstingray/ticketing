"""Security settings routes — server-managed, admin-editable settings that
affect the app's security posture (the webhook SSRF exemption list, the
insecure-webhooks/dispatcher-pause toggles, the lease TTL policy window, and
the per-user webhook cap).

A single global row, unlike resolver settings' per-bot keying — these are
app-wide policy, not per-identity tunables. Both GET and PUT are gated behind
``require_recent_admin`` (admin role AND a session cookie minted within the
last few minutes, see auth.py) rather than just ``require_admin`` — even
*viewing* this panel requires a fresh login, since it's meant to be a
step-up surface end to end, not merely a write-gated one.

``get_security_settings`` is the read path the rest of the backend calls
(webhook_urls, the dispatcher, the claim route, the webhooks-count check) to
read the *effective* settings — it takes no request/auth dependency, since
those call sites aren't behind an admin request.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from auth import require_recent_admin
from database import get_db
from models import SecuritySettings, User
from schemas import SecuritySettingsOut, SecuritySettingsUpdate, SecuritySettingsValues

router = APIRouter(prefix="/security-settings", tags=["security-settings"])

# Single global row.
_ROW_ID = 1


def _row(db: Session) -> SecuritySettings | None:
    return db.query(SecuritySettings).filter(SecuritySettings.id == _ROW_ID).one_or_none()


def get_security_settings(db: Session) -> SecuritySettingsValues:
    """The effective settings (defaults <- stored row). Call this from
    anywhere in the backend that needs to read the current policy — no auth
    dependency, since most call sites (webhook validation, the dispatcher
    loop, ticket claims) aren't themselves behind an admin request."""
    row = _row(db)
    if row is None or not row.settings:
        return SecuritySettingsValues()
    return SecuritySettingsValues(**row.settings)


def _out(db: Session) -> SecuritySettingsOut:
    row = _row(db)
    return SecuritySettingsOut(
        settings=get_security_settings(db),
        updated_at=row.updated_at if row else None,
        updated_by=row.updated_by if row else None,
    )


@router.get("", response_model=SecuritySettingsOut)
def get_settings(
    db: Session = Depends(get_db),
    _user: User = Depends(require_recent_admin),
):
    return _out(db)


@router.put("", response_model=SecuritySettingsOut)
def update_settings(
    payload: SecuritySettingsUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_recent_admin),
):
    # Only the fields the admin actually sent are persisted (partial update),
    # merged onto whatever is already stored.
    changes = payload.model_dump(exclude_unset=True, mode="json")
    row = _row(db)
    if row is None:
        row = SecuritySettings(id=_ROW_ID, settings={})
        db.add(row)
    stored = dict(row.settings or {})
    stored.update(changes)
    # Validate the MERGED result as a whole (cross-field lease-window bounds
    # can't be checked on a partial payload — a PUT may send only one of the
    # three lease fields). A bad merged state is a 422, not a corrupted row.
    try:
        SecuritySettingsValues(**stored)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    row.settings = stored
    row.updated_by = admin.id
    # settings is a JSON column mutated in place above; reassigning ensures the
    # ORM flags it dirty. updated_at is refreshed by the model's onupdate.
    db.commit()
    return _out(db)
