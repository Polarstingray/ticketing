"""In-memory brute-force throttles for the auth paths.

Two small, thread-safe throttles backed by plain dicts:

* :data:`account_lockout` — per-account (lowercased username) failure counter
  with exponential backoff / temporary lockout, used by ``POST /auth/login`` to
  blunt distributed / credential-stuffing attacks against a single account that
  per-IP limiting alone cannot stop.
* :data:`api_key_throttle` — per-IP failed ``X-API-Key`` counter (fixed window)
  used by ``get_current_user`` as defense-in-depth against key guessing.

**Bounded memory:** both maps are keyed on unauthenticated, caller-supplied
strings (a username, a client IP), so they must never grow without limit. Every
entry is therefore disposable once it has aged out — expired entries are swept
opportunistically on write, and a hard ``max_entries`` cap evicts the stalest
entries if a flood outruns the sweep. Eviction prefers entries that are already
harmless (expired window / no active lockout) precisely because evicting a live
entry is what an attacker would want: it resets somebody's counter.

**Trade-off:** this state lives in process memory, so it is per-process and lost
on restart. That is correct for the current single-uvicorn-worker container
(``backend/Dockerfile`` runs uvicorn with no ``--workers``; the Fly demo is
pinned with ``fly scale count 1``). Two consequences worth stating outright:

* The invariant is upheld by convention across those files, not by code. Adding
  ``--workers``/replicas silently splits these counters per process and turns
  every limit here into "limit × number of workers".
* The Fly demo scales to zero when idle, so all lockout state is wiped whenever
  the machine sleeps. Sustained attack traffic keeps it warm, so this is not a
  live-attack bypass, but a lockout is not durable there.

If the app is scaled out, move this state (and the slowapi storage) to Redis, or
persist counters in ``User.failed_login_attempts`` / ``locked_until`` columns.
New DB columns are avoided here because the app uses ``Base.metadata.create_all``
with no migration tooling, so altering the existing SQLite table would require a
manual migration.
"""
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Soft/hard cap on how many distinct keys either throttle will track. Well above
# any plausible legitimate working set (distinct usernames failing a login, or
# distinct client IPs failing key auth, within one window), so it only ever bites
# during a flood of made-up keys.
MAX_TRACKED_KEYS = 10_000


@dataclass
class _AccountState:
    failed_count: int = 0
    locked_until: float = 0.0  # epoch seconds; 0 means not locked
    last_failure_at: float = 0.0


class AccountLockout:
    """Per-account exponential-backoff lockout.

    After ``threshold`` consecutive failures an account is locked for a window
    that doubles with each further failure, capped at ``cap_seconds``.

    ``decay_seconds`` bounds the obvious abuse of a per-account lockout: anyone
    who knows a username can keep that account locked by failing logins, and the
    victim cannot clear the counter, because clearing it requires the successful
    login they are being denied. The failure counter is therefore forgotten after
    ``decay_seconds`` of quiet. That caps the ratchet: once the backoff tier
    exceeds ``decay_seconds``, the account is quiet for longer than the decay
    window while it sits locked, so the counter resets when the lockout lifts and
    the attacker has to spend ``threshold`` fresh failures (against the per-IP
    limits) to climb again — instead of holding a ``cap_seconds`` lock forever on
    one request an hour. Bursts fast enough to matter for credential stuffing are
    unaffected, since they land well inside the decay window.
    """

    def __init__(
        self,
        threshold: int = 5,
        base_seconds: float = 30.0,
        cap_seconds: float = 3600.0,
        decay_seconds: float = 900.0,
        max_entries: int = MAX_TRACKED_KEYS,
        time_fn: Callable[[], float] = time.time,
    ):
        self.threshold = threshold
        self.base_seconds = base_seconds
        self.cap_seconds = cap_seconds
        self.decay_seconds = decay_seconds
        self.max_entries = max_entries
        self._now = time_fn
        self._states: dict[str, _AccountState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(username: str) -> str:
        # Load-bearing: the login lookup is case-SENSITIVE (`User.username ==
        # payload.username`) while this bucket is case-insensitive. That
        # mismatch is deliberate and safe in this direction only — "Alice" and
        # "alice" share one lockout bucket, so case-toggling cannot multiply an
        # attacker's attempt budget. Do not "simplify" this to match the query.
        return username.strip().lower()

    def _is_stale(self, state: _AccountState, now: float) -> bool:
        """True when an entry carries no live lockout and no live counter."""
        return state.locked_until <= now and state.last_failure_at + self.decay_seconds <= now

    def _evict(self, now: float) -> None:
        """Bound ``_states``. Caller must hold ``self._lock``.

        Drops aged-out entries first; only if that is not enough does it fall
        back to evicting the least-recently-failed live entries, which is the
        lesser evil versus unbounded growth.
        """
        if len(self._states) <= self.max_entries:
            return
        for key in [k for k, s in self._states.items() if self._is_stale(s, now)]:
            del self._states[key]
        if len(self._states) <= self.max_entries:
            return
        # Still over the cap: an attacker is flooding distinct usernames. Shed
        # entries that are merely counting failures before any that hold a live
        # lockout — dropping a lockout is exactly the outcome the flood would be
        # aiming for — and oldest-first within each group.
        overflow = len(self._states) - self.max_entries
        oldest = sorted(
            self._states.items(),
            key=lambda kv: (kv[1].locked_until > now, kv[1].last_failure_at),
        )[:overflow]
        for key, _ in oldest:
            del self._states[key]

    def retry_after(self, username: str) -> int:
        """Return seconds remaining on an active lockout, or 0 if not locked."""
        key = self._key(username)
        now = self._now()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return 0
            if state.locked_until > now:
                return int(state.locked_until - now) + 1
            # Not locked; drop the entry if it has also aged out, so accounts
            # that fail once and never come back don't linger forever.
            if self._is_stale(state, now):
                del self._states[key]
        return 0

    def register_failure(self, username: str) -> None:
        """Record a failed attempt and, past the threshold, (re)arm the lockout."""
        key = self._key(username)
        now = self._now()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _AccountState()
                self._states[key] = state
            elif self._is_stale(state, now):
                # Quiet for longer than the decay window: start over.
                state.failed_count = 0
                state.locked_until = 0.0
            state.failed_count += 1
            state.last_failure_at = now
            if state.failed_count >= self.threshold:
                # n = number of failures at or beyond the threshold (1, 2, 3, ...)
                n = state.failed_count - self.threshold
                backoff = min(self.base_seconds * (2 ** n), self.cap_seconds)
                state.locked_until = now + backoff
                # Locked-out users are otherwise a silent support ticket, and a
                # burst of these is the signature of a credential-stuffing run.
                # The username is caller-supplied, so log the normalized key and
                # nothing else (never the submitted password).
                logger.warning(
                    "account lockout armed for %r: %d consecutive failures, locked %ds",
                    key, state.failed_count, int(backoff),
                )
            self._evict(now)

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
    """Per-IP fixed-window failure counter (used for the API-key path).

    Fixed windows are cheap and approximate: an attacker who lines up with the
    boundary can land ``max_failures`` at the end of one window and
    ``max_failures`` at the start of the next, i.e. up to ``2 × max_failures``
    in quick succession. That worst case is accepted here — at 20/minute against
    high-entropy key hashes it is not a meaningful edge. A sliding-window log
    (or a two-bucket current+previous weighted counter) is the fix if an exact
    bound is ever needed.
    """

    def __init__(
        self,
        max_failures: int = 20,
        window_seconds: float = 60.0,
        max_entries: int = MAX_TRACKED_KEYS,
        time_fn: Callable[[], float] = time.time,
    ):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.max_entries = max_entries
        self._now = time_fn
        self._states: dict[str, _WindowState] = {}
        self._lock = threading.Lock()

    def _expired(self, state: _WindowState, now: float) -> bool:
        return now - state.window_start >= self.window_seconds

    def _evict(self, now: float) -> None:
        """Bound ``_states``. Caller must hold ``self._lock``."""
        if len(self._states) <= self.max_entries:
            return
        for key in [k for k, s in self._states.items() if self._expired(s, now)]:
            del self._states[key]
        if len(self._states) <= self.max_entries:
            return
        # As in AccountLockout: shed windows that aren't blocking anything before
        # windows that are, oldest-first within each group.
        overflow = len(self._states) - self.max_entries
        oldest = sorted(
            self._states.items(),
            key=lambda kv: (kv[1].count >= self.max_failures, kv[1].window_start),
        )[:overflow]
        for key, _ in oldest:
            del self._states[key]

    def is_blocked(self, ip: str) -> bool:
        now = self._now()
        with self._lock:
            state = self._states.get(ip)
            if not state:
                return False
            if self._expired(state, now):
                # Window expired: drop the stale entry rather than leaving it for
                # the next failure to re-base, so IPs that never come back don't
                # accumulate. Behaviorally identical (unblocked either way).
                del self._states[ip]
                return False
            return state.count >= self.max_failures

    def register_failure(self, ip: str) -> None:
        now = self._now()
        with self._lock:
            state = self._states.get(ip)
            if not state or self._expired(state, now):
                self._states[ip] = _WindowState(count=1, window_start=now)
            else:
                state.count += 1
            self._evict(now)

    def register_success(self, ip: str) -> None:
        """Credit back one failure after a successful key auth.

        Deliberately not a reset: the key is authenticated, the *IP* is not, so
        one good request from a shared NAT (or from an attacker who holds any
        single valid key) must not hand back the whole budget for bad guesses
        interleaved from the same address. The window expires on its own anyway.
        """
        with self._lock:
            state = self._states.get(ip)
            if state is None:
                return
            state.count -= 1
            if state.count <= 0:
                del self._states[ip]


# Module-level singletons shared across requests.
account_lockout = AccountLockout()
api_key_throttle = IpFailureThrottle()
