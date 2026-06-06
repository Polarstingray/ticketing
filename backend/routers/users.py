"""User management routes (mostly admin-only)."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import (
    generate_api_key,
    get_current_user,
    hash_password,
    is_admin,
    require_admin,
)
from database import get_db
from models import User, UserRole
from schemas import ApiKeyOut, UserCreate, UserPublic, UserSelf, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


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
        api_key=generate_api_key(),
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
    credentials_changed = False
    if "password" in data and data["password"] is not None:
        user.hashed_password = hash_password(data["password"])
        credentials_changed = True
    if "role" in data and data["role"] is not None:
        # Only admins may change roles.
        if not is_admin(current):
            raise HTTPException(status_code=403, detail="Only admins may change roles")
        if user.role != data["role"].value:
            credentials_changed = True
        user.role = data["role"].value

    # A password or role change must invalidate the target user's existing
    # sessions so a reset/leaked cookie can't keep being used.
    if credentials_changed:
        user.session_version += 1

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


@router.post("/{user_id}/regenerate-api-key", response_model=ApiKeyOut)
def regenerate_api_key(
    user_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    if not is_admin(current) and current.id != user_id:
        raise HTTPException(status_code=403, detail="Not permitted")
    user = _get_user_or_404(user_id, db)
    user.api_key = generate_api_key()
    db.commit()
    db.refresh(user)
    return ApiKeyOut(api_key=user.api_key)
