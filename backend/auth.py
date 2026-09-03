"""Authentication helpers: password hashing, signed-cookie sessions, API keys,
and FastAPI dependencies for resolving the current user / enforcing roles."""
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from database import get_db
from login_throttle import api_key_throttle
from models import ApiKey, Ticket, User, UserRole, utcnow

# How stale last_used_at may get before we bother writing it again (avoids a DB
# write on every single API request).
API_KEY_TOUCH_INTERVAL = timedelta(seconds=60)

# --- Configuration -----------------------------------------------------------

# No hardcoded fallback: a default baked into the source is a *public* signing
# key, so anyone holding the repo could forge session cookies for any user. When
# SESSION_SECRET is unset we mint a random one for this process instead — dev
# still boots without configuration, but the key is not knowable, and sessions
# simply do not survive a restart. startup.py turns the unset case into a fatal
# error in production (and a warning in dev) via SESSION_SECRET_IS_EPHEMERAL.
_env_session_secret = os.environ.get("SESSION_SECRET", "")
SESSION_SECRET_IS_EPHEMERAL = not _env_session_secret
SESSION_SECRET = _env_session_secret or secrets.token_urlsafe(48)
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


def hash_api_key(raw: str) -> str:
    """Deterministic hash used to look up a key without storing the plaintext."""
    return hashlib.sha256(raw.encode()).hexdigest()


# --- Sessions (stateless signed cookie) --------------------------------------

def create_session_token(user_id: int, session_version: int) -> str:
    return _serializer.dumps({"user_id": user_id, "sv": session_version})


def read_session_token(token: str, *, with_timestamp: bool = False):
    """Return the decoded token payload ({"user_id", "sv"}), or None if the
    signature is bad/expired.

    With ``with_timestamp=True``, return ``(payload, issued_at)`` instead —
    ``issued_at`` is a UTC-aware datetime of when the token was minted,
    reusing itsdangerous's own embedded timestamp (already used internally
    for the ``max_age`` check above) rather than adding a redundant custom
    claim. ``require_recent_admin`` uses this to gate the security-settings
    panel behind a fresh login, not just a currently-valid session."""
    try:
        if with_timestamp:
            return _serializer.loads(token, max_age=SESSION_MAX_AGE, return_timestamp=True)
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return (None, None) if with_timestamp else None


def set_session_cookie(response, user: User):
    response.set_cookie(
        key=SESSION_COOKIE,
        value=create_session_token(user.id, user.session_version),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )


def clear_session_cookie(response):
    response.delete_cookie(SESSION_COOKIE, path="/")


# --- Dependencies ------------------------------------------------------------

def _expired(expires_at) -> bool:
    if expires_at is None:
        return False
    # DB values come back naive (UTC); normalize an aware value just in case.
    if expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
    return expires_at <= datetime.utcnow()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Resolve the current user from the X-API-Key header or the session cookie.

    The API key is checked first so programmatic clients (Claude Code) work even
    if a stale browser cookie is also present.
    """
    # Always define these, including on the cookie and 401 paths, so
    # get_api_key/require_recent_admin never see a missing attribute.
    request.state.api_key = None
    request.state.session_issued_at = None

    raw_key = request.headers.get("X-API-Key")
    if raw_key:
        # Defense-in-depth against key guessing: cap bad-key attempts per IP.
        # High-entropy keys already make guessing impractical; this just denies
        # an attacker unlimited tries (and the DB lookup they'd cost).
        client_ip = request.client.host if request.client else "unknown"
        if api_key_throttle.is_blocked(client_ip):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many invalid API key attempts; try again later",
            )
        key = (
            db.query(ApiKey)
            .filter(ApiKey.key_hash == hash_api_key(raw_key))
            .first()
        )
        if key and not key.revoked and not _expired(key.expires_at):
            api_key_throttle.register_success(client_ip)
            # Throttle last_used_at writes so we don't touch the DB every request.
            now = utcnow()
            last = key.last_used_at
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last is None or (now - last) > API_KEY_TOUCH_INTERVAL:
                key.last_used_at = now
                db.commit()
            # Stash the key itself so scope-aware checks can reach it; the return
            # type stays `User` because ~7 routers depend on this. See get_api_key.
            request.state.api_key = key
            return key.user
        api_key_throttle.register_failure(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        data, issued_at = read_session_token(token, with_timestamp=True)
        # Require both a user_id and a session version. Tokens minted before
        # revocable sessions lacked "sv", so they are rejected here too.
        if data is not None and "user_id" in data and "sv" in data:
            user = db.query(User).filter(User.id == data["user_id"]).first()
            if user and user.session_version == data["sv"]:
                request.state.session_issued_at = issued_at
                return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
    )


def get_api_key(
    request: Request, _user: User = Depends(get_current_user)
) -> ApiKey | None:
    """The ApiKey that authenticated this request, or None for a cookie session.

    The ``get_current_user`` dependency is load-bearing for *ordering*, not for its
    value: it is what sets ``request.state.api_key``, and FastAPI caches it per
    request so depending on it here costs nothing. Without it, resolution order is
    undefined and the attribute may not be set yet. Do not "clean it up".
    """
    return getattr(request.state, "api_key", None)


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.admin.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return user


def is_admin(user: User) -> bool:
    return user.role == UserRole.admin.value


# How fresh a login has to be to reach the security-settings panel. Deliberately
# a code constant, not a DB-editable setting: if it lived in the very table this
# gate protects, a stale-but-valid admin session could weaken its own gate
# without ever proving a fresh login.
REAUTH_WINDOW_SECONDS = 15 * 60


def require_recent_admin(request: Request, user: User = Depends(require_admin)) -> User:
    """Admin AND the session cookie was minted within REAUTH_WINDOW_SECONDS.

    Not satisfiable via an API key: request.state.session_issued_at is only
    set on the cookie path (see get_current_user), so this dependency is a
    browser/UI-only surface by construction — a programmatic caller gets 401
    regardless of how recently its key was created, since key age isn't login
    recency. Distinguished from a bare "not authenticated" 401 by the
    "reauth_required" detail, which the frontend uses to show an inline
    re-login prompt instead of a generic error.
    """
    issued_at = getattr(request.state, "session_issued_at", None)
    if issued_at is None or (utcnow() - issued_at).total_seconds() > REAUTH_WINDOW_SECONDS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="reauth_required")
    return user


def can_modify_ticket(user: User, ticket: Ticket) -> bool:
    """Admins may modify any ticket; members may modify tickets they created or
    are assigned to."""
    if is_admin(user):
        return True
    return user.id in (ticket.created_by, ticket.assigned_to)


def can_view_ticket(user: User, ticket: Ticket) -> bool:
    """Admins may view any ticket; members may view tickets they created or are
    assigned to. Read access is intentionally as narrow as modify access because
    code_review tickets embed private source in code_blocks."""
    if is_admin(user):
        return True
    return user.id in (ticket.created_by, ticket.assigned_to)
