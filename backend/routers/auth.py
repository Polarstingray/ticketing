"""Authentication routes: login, logout, current-user."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from auth import (
    SESSION_COOKIE,
    clear_session_cookie,
    get_current_user,
    read_session_token,
    set_session_cookie,
    verify_password_or_dummy,
)
from database import get_db
from login_throttle import account_lockout
from models import User
from ratelimit import limiter
from schemas import LoginRequest, UserSelf

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=UserSelf)
# Per-IP limit (slowapi). Stops offline-speed network brute force from a single
# source while leaving headroom for fat-fingered passwords. slowapi requires the
# endpoint to have a parameter named exactly ``request``.
@limiter.limit("5/minute;30/hour")
def login(request: Request, payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    # Per-account lockout (covers distributed / credential-stuffing attacks that
    # spread across IPs and so slip past the per-IP limit above).
    retry_after = account_lockout.retry_after(payload.username)
    if retry_after:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Account temporarily locked due to too many failed attempts",
            headers={"Retry-After": str(retry_after)},
        )

    user = db.query(User).filter(User.username == payload.username).first()
    # Always run exactly one bcrypt verify (dummy hash when the user is missing)
    # so response timing can't reveal whether the username exists.
    ok = verify_password_or_dummy(payload.password, user.hashed_password if user else None)
    if not user or not ok:
        account_lockout.register_failure(payload.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    account_lockout.register_success(payload.username)
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
