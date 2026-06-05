"""Authentication helpers: password hashing, signed-cookie sessions, API keys,
and FastAPI dependencies for resolving the current user / enforcing roles."""
import os
import secrets

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from database import get_db
from login_throttle import api_key_throttle
from models import Ticket, User, UserRole

# --- Configuration -----------------------------------------------------------

SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-change-me")
SESSION_COOKIE = "session"
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", 60 * 60 * 24 * 14))  # 14 days
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="session")


# --- Passwords ---------------------------------------------------------------

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)


# A throwaway hash computed once at import. Verifying against it on the
# user-not-found branch burns the same bcrypt time as a real verify, so the
# response time can't be used to enumerate which usernames exist.
_DUMMY_HASH = pwd_context.hash("dummy-password-for-timing")


def verify_password_or_dummy(password: str, hashed: str | None) -> bool:
    """Like :func:`verify_password`, but always runs one bcrypt verify.

    When ``hashed`` is ``None`` (no such user) it verifies against a dummy hash
    and returns ``False``, equalizing timing with the wrong-password case.
    """
    if hashed is None:
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, hashed)


# --- API keys ----------------------------------------------------------------

def generate_api_key() -> str:
    return "sk_" + secrets.token_urlsafe(32)


# --- Sessions (stateless signed cookie) --------------------------------------

def create_session_token(user_id: int) -> str:
    return _serializer.dumps({"user_id": user_id})


def read_session_token(token: str):
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return data.get("user_id")
    except (BadSignature, SignatureExpired):
        return None


def set_session_cookie(response, user_id: int):
    response.set_cookie(
        key=SESSION_COOKIE,
        value=create_session_token(user_id),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )


def clear_session_cookie(response):
    response.delete_cookie(SESSION_COOKIE, path="/")


# --- Dependencies ------------------------------------------------------------

def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Resolve the current user from the X-API-Key header or the session cookie.

    The API key is checked first so programmatic clients (Claude Code) work even
    if a stale browser cookie is also present.
    """
    api_key = request.headers.get("X-API-Key")
    if api_key:
        # Defense-in-depth against key guessing: cap bad-key attempts per IP.
        # High-entropy keys already make guessing impractical; this just denies
        # an attacker unlimited tries (and the DB lookup they'd cost).
        client_ip = request.client.host if request.client else "unknown"
        if api_key_throttle.is_blocked(client_ip):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many invalid API key attempts; try again later",
            )
        user = db.query(User).filter(User.api_key == api_key).first()
        if user:
            api_key_throttle.register_success(client_ip)
            return user
        api_key_throttle.register_failure(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        user_id = read_session_token(token)
        if user_id is not None:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
    )


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.admin.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return user


def is_admin(user: User) -> bool:
    return user.role == UserRole.admin.value


def can_modify_ticket(user: User, ticket: Ticket) -> bool:
    """Admins may modify any ticket; members may modify tickets they created or
    are assigned to."""
    if is_admin(user):
        return True
    return user.id in (ticket.created_by, ticket.assigned_to)
