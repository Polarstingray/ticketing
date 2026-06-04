"""Authentication routes: login, logout, current-user."""
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from auth import (
    clear_session_cookie,
    get_current_user,
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
    set_session_cookie(response, user.id)
    return user


@router.post("/logout")
def logout(response: Response):
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me", response_model=UserSelf)
def me(user: User = Depends(get_current_user)):
    return user
