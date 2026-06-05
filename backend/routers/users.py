"""User management routes (mostly admin-only)."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import (
    generate_api_key,
    get_current_user,
    hash_api_key,
    hash_password,
    is_admin,
    require_admin,
)
from database import get_db
from models import ApiKey, User, utcnow
from schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyMeta,
    UserCreate,
    UserPublic,
    UserSelf,
    UserUpdate,
)

router = APIRouter(prefix="/users", tags=["users"])


def _require_self_or_admin(current: User, user_id: int) -> None:
    if not is_admin(current) and current.id != user_id:
        raise HTTPException(status_code=403, detail="Not permitted")


def _get_user_or_404(user_id: int, db: Session) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.get("", response_model=list[UserPublic])
def list_users(db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    return db.query(User).order_by(User.created_at.asc()).all()


@router.post("", response_model=UserSelf, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    user = User(
        username=payload.username,
        display_name=payload.display_name,
        email=payload.email,
        role=payload.role.value,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Username already exists")
    db.refresh(user)
    return user


@router.patch("/{user_id}", response_model=UserSelf)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    # A user may edit themselves; admins may edit anyone.
    if not is_admin(current) and current.id != user_id:
        raise HTTPException(status_code=403, detail="Not permitted")

    user = _get_user_or_404(user_id, db)
    data = payload.model_dump(exclude_unset=True)

    if "display_name" in data and data["display_name"] is not None:
        user.display_name = data["display_name"]
    if "email" in data and data["email"] is not None:
        user.email = data["email"]
    if "password" in data and data["password"] is not None:
        user.hashed_password = hash_password(data["password"])
    if "role" in data and data["role"] is not None:
        # Only admins may change roles.
        if not is_admin(current):
            raise HTTPException(status_code=403, detail="Only admins may change roles")
        user.role = data["role"].value

    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = _get_user_or_404(user_id, db)
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    db.delete(user)
    db.commit()
    return None


# --- API keys ----------------------------------------------------------------

@router.get("/{user_id}/api-keys", response_model=list[ApiKeyMeta])
def list_api_keys(
    user_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    _require_self_or_admin(current, user_id)
    _get_user_or_404(user_id, db)
    return (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id)
        .order_by(ApiKey.created_at.desc())
        .all()
    )


@router.post("/{user_id}/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
def create_api_key(
    user_id: int,
    payload: ApiKeyCreate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    _require_self_or_admin(current, user_id)
    _get_user_or_404(user_id, db)

    raw = generate_api_key()
    expires_at = (
        utcnow() + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days
        else None
    )
    key = ApiKey(
        user_id=user_id,
        name=payload.name,
        key_prefix=raw[:11],
        key_hash=hash_api_key(raw),
        expires_at=expires_at,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    # `api_key` (plaintext) is returned exactly once, here.
    return ApiKeyCreated(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        expires_at=key.expires_at,
        revoked=key.revoked,
        api_key=raw,
    )


@router.post("/{user_id}/api-keys/{key_id}/revoke", response_model=ApiKeyMeta)
def revoke_api_key(
    user_id: int,
    key_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    _require_self_or_admin(current, user_id)
    key = (
        db.query(ApiKey)
        .filter(ApiKey.id == key_id, ApiKey.user_id == user_id)
        .first()
    )
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    key.revoked = True
    db.commit()
    db.refresh(key)
    return key
