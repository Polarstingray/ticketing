"""Authentication routes: login, logout, current-user."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from auth import (
    SESSION_COOKIE,
    clear_session_cookie,
    get_current_user,
    read_session_token,
    set_session_cookie,
    verify_password,
)
from database import get_db
from models import User
from schemas import LoginRequest, UserSelf

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=UserSelf)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    set_session_cookie(response, user)
    return user


@router.post("/logout")
def logout(
    response: Response,
    request: Request,
    db: Session = Depends(get_db),
):
    # Bump the session version of the authenticated user so the cookie that was
    # just cleared (and any leaked copies of it) can no longer be reused. We
    # resolve the user manually rather than via Depends(get_current_user) so an
    # already-unauthenticated logout still succeeds instead of returning 401.
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        data = read_session_token(token)
        if data is not None and "user_id" in data:
            user = db.query(User).filter(User.id == data["user_id"]).first()
            if user:
                user.session_version += 1
                db.commit()
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me", response_model=UserSelf)
def me(user: User = Depends(get_current_user)):
    return user
