"""In-memory brute-force throttles for the auth paths.

Two small, thread-safe throttles backed by plain dicts:

* :data:`account_lockout` — per-account (lowercased username) failure counter
  with exponential backoff / temporary lockout, used by ``POST /auth/login`` to
  blunt distributed / credential-stuffing attacks against a single account that
  per-IP limiting alone cannot stop.
* :data:`api_key_throttle` — per-IP failed ``X-API-Key`` counter (fixed window)
  used by ``get_current_user`` as defense-in-depth against key guessing.

**Trade-off:** this state lives in process memory, so it is per-process and lost
on restart. That is correct for the current single-uvicorn-worker container. If
the app is scaled to multiple workers/replicas, move this state (and the slowapi
storage) to Redis, or persist counters in ``User.failed_login_attempts`` /
``locked_until`` columns. New DB columns are avoided here because the app uses
``Base.metadata.create_all`` with no migration tooling, so altering the existing
SQLite table would require a manual migration.
"""
import threading
import time
from dataclasses import dataclass, field


@dataclass
class _AccountState:
    failed_count: int = 0
    locked_until: float = 0.0  # epoch seconds; 0 means not locked


class AccountLockout:
    """Per-account exponential-backoff lockout.

    After ``threshold`` consecutive failures an account is locked for a window
    that doubles with each further failure, capped at ``cap_seconds``.
    """

    def __init__(self, threshold: int = 5, base_seconds: float = 30.0, cap_seconds: float = 3600.0):
        self.threshold = threshold
        self.base_seconds = base_seconds
        self.cap_seconds = cap_seconds
        self._states: dict[str, _AccountState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(username: str) -> str:
        return username.strip().lower()

    def retry_after(self, username: str) -> int:
        """Return seconds remaining on an active lockout, or 0 if not locked."""
        key = self._key(username)
        now = time.time()
        with self._lock:
            state = self._states.get(key)
            if state and state.locked_until > now:
                return int(state.locked_until - now) + 1
        return 0

    def register_failure(self, username: str) -> None:
        """Record a failed attempt and, past the threshold, (re)arm the lockout."""
        key = self._key(username)
        now = time.time()
        with self._lock:
            state = self._states.setdefault(key, _AccountState())
            state.failed_count += 1
            if state.failed_count >= self.threshold:
                # n = number of failures at or beyond the threshold (1, 2, 3, ...)
                n = state.failed_count - self.threshold
                backoff = min(self.base_seconds * (2 ** n), self.cap_seconds)
                state.locked_until = now + backoff

    def register_success(self, username: str) -> None:
        """Clear all failure state for the account on a successful login."""
        key = self._key(username)
        with self._lock:
            self._states.pop(key, None)


@dataclass
class _WindowState:
    count: int = 0
    window_start: float = field(default_factory=time.time)


class IpFailureThrottle:
    """Per-IP fixed-window failure counter (used for the API-key path)."""

    def __init__(self, max_failures: int = 20, window_seconds: float = 60.0):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._states: dict[str, _WindowState] = {}
        self._lock = threading.Lock()

    def is_blocked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            state = self._states.get(ip)
            if not state:
                return False
            if now - state.window_start >= self.window_seconds:
                # Window expired; treat as a fresh, unblocked window.
                return False
            return state.count >= self.max_failures

    def register_failure(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            state = self._states.get(ip)
            if not state or now - state.window_start >= self.window_seconds:
                self._states[ip] = _WindowState(count=1, window_start=now)
            else:
                state.count += 1

    def register_success(self, ip: str) -> None:
        with self._lock:
            self._states.pop(ip, None)


# Module-level singletons shared across requests.
account_lockout = AccountLockout()
api_key_throttle = IpFailureThrottle()
