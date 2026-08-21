"""Saved dashboard views: named, reusable ticket-list filters.

Every route here is scoped to the authenticated user — a view is looked up by
(id, user_id), never by id alone, so one user can neither read nor mutate
another's. A view that exists but belongs to someone else returns 404 rather
than 403: a distinct 403 would confirm the id exists, and there is nothing a
caller could do with that fact except enumerate.

The stored `query` is the dashboard's raw URL query string. It is opaque here —
validated for length/charset in schemas, then echoed back verbatim and applied
client-side. See models.SavedView.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import SavedView, User
from schemas import (
    MAX_SAVED_VIEWS,
    SavedViewCreate,
    SavedViewOut,
    SavedViewUpdate,
)

router = APIRouter(prefix="/saved-views", tags=["saved-views"])


def _own_view_or_404(view_id: int, db: Session, user: User) -> SavedView:
    view = (
        db.query(SavedView)
        .filter(SavedView.id == view_id, SavedView.user_id == user.id)
        .first()
    )
    if not view:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="View not found")
    return view


def _reject_duplicate_name(db: Session, user: User, name: str, exclude_id: int | None = None):
    """409 if this user already has a view by that name.

    There is a UNIQUE(user_id, name) constraint behind this; checking first turns
    what would be a 500 IntegrityError into a useful status code.
    """
    q = db.query(SavedView).filter(SavedView.user_id == user.id, SavedView.name == name)
    if exclude_id is not None:
        q = q.filter(SavedView.id != exclude_id)
    if q.first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A saved view named {name!r} already exists",
        )


@router.get("", response_model=list[SavedViewOut])
def list_saved_views(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return (
        db.query(SavedView)
        .filter(SavedView.user_id == user.id)
        .order_by(SavedView.name.asc())
        .all()
    )


@router.post("", response_model=SavedViewOut, status_code=status.HTTP_201_CREATED)
def create_saved_view(
    payload: SavedViewCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _reject_duplicate_name(db, user, payload.name)
    # Bounded per user: these are cheap to create from the UI, and an unbounded
    # list is both a storage concern and an unusable picker.
    count = db.query(SavedView).filter(SavedView.user_id == user.id).count()
    if count >= MAX_SAVED_VIEWS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Too many saved views (max {MAX_SAVED_VIEWS}); delete one first",
        )

    view = SavedView(user_id=user.id, name=payload.name, query=payload.query)
    db.add(view)
    db.commit()
    db.refresh(view)
    return view


@router.patch("/{view_id}", response_model=SavedViewOut)
def update_saved_view(
    view_id: int,
    payload: SavedViewUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    view = _own_view_or_404(view_id, db, user)
    if payload.name is not None and payload.name != view.name:
        _reject_duplicate_name(db, user, payload.name, exclude_id=view.id)
        view.name = payload.name
    if payload.query is not None:
        view.query = payload.query
    db.commit()
    db.refresh(view)
    return view


@router.delete("/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved_view(
    view_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    view = _own_view_or_404(view_id, db, user)
    db.delete(view)
    db.commit()
    return None
