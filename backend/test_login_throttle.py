"""Unit tests for the in-memory auth throttles.

Both classes take an injectable clock, so nothing here sleeps: the tests drive
``fake_clock`` forward and assert on the resulting state. Covered: the backoff
doubling and its cap, the decay that stops a lockout from being held forever,
the fixed-window rollover, and the eviction that keeps ``_states`` bounded
against attacker-chosen keys.
"""
import pytest

from login_throttle import AccountLockout, IpFailureThrottle


class Clock:
    """Manually advanced stand-in for ``time.time``."""

    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


# --- AccountLockout ----------------------------------------------------------

def test_lockout_arms_only_at_threshold(clock):
    lockout = AccountLockout(threshold=3, base_seconds=30, time_fn=clock)
    for _ in range(2):
        lockout.register_failure("alice")
    assert lockout.retry_after("alice") == 0

    lockout.register_failure("alice")
    # +1 because retry_after rounds up so a client that waits it out is clear.
    assert lockout.retry_after("alice") == 31


def test_backoff_doubles_per_failure_and_caps(clock):
    lockout = AccountLockout(threshold=1, base_seconds=30, cap_seconds=120, time_fn=clock)
    for expected in (30, 60, 120, 120):
        lockout.register_failure("alice")
        assert lockout.retry_after("alice") == expected + 1
        clock.advance(expected + 1)


def test_lockout_expires_on_its_own(clock):
    lockout = AccountLockout(threshold=1, base_seconds=30, time_fn=clock)
    lockout.register_failure("alice")
    clock.advance(31)
    assert lockout.retry_after("alice") == 0


def test_success_clears_failure_state(clock):
    lockout = AccountLockout(threshold=3, base_seconds=30, time_fn=clock)
    lockout.register_failure("alice")
    lockout.register_failure("alice")
    lockout.register_success("alice")
    assert lockout._states == {}

    # The counter really restarted: two more failures still don't lock.
    lockout.register_failure("alice")
    lockout.register_failure("alice")
    assert lockout.retry_after("alice") == 0


def test_key_is_case_insensitive_and_stripped(clock):
    lockout = AccountLockout(threshold=2, base_seconds=30, time_fn=clock)
    lockout.register_failure("Alice")
    lockout.register_failure("  alice ")
    # Case-toggling must not buy a fresh attempt budget.
    assert lockout.retry_after("ALICE") == 31


def test_counter_decays_after_a_quiet_window(clock):
    lockout = AccountLockout(threshold=3, base_seconds=30, decay_seconds=900, time_fn=clock)
    lockout.register_failure("alice")
    lockout.register_failure("alice")

    clock.advance(901)
    # The stale failures are forgotten, so this is failure #1, not #3.
    lockout.register_failure("alice")
    assert lockout.retry_after("alice") == 0


def test_slow_attacker_cannot_ratchet_the_lockout_forever(clock):
    """Once a tier outlasts the decay window, the counter resets when it lifts.

    This is what stops "one failed login an hour" from holding somebody's
    account at the cap_seconds lockout indefinitely.
    """
    lockout = AccountLockout(
        threshold=1, base_seconds=600, cap_seconds=3600, decay_seconds=900, time_fn=clock
    )
    lockout.register_failure("alice")   # 600s lock, shorter than the decay window
    clock.advance(601)
    lockout.register_failure("alice")   # still ratchets: 1200s
    assert lockout.retry_after("alice") == 1201

    clock.advance(1201)                 # quiet for > decay_seconds while locked
    lockout.register_failure("alice")
    # Back to the first tier instead of climbing toward the 3600s cap.
    assert lockout.retry_after("alice") == 601


def test_expired_entries_are_dropped(clock):
    lockout = AccountLockout(threshold=5, decay_seconds=900, time_fn=clock)
    lockout.register_failure("alice")
    clock.advance(901)

    # A read of the aged-out account cleans it up...
    assert lockout.retry_after("alice") == 0
    assert lockout._states == {}

    # ...and so does the next write, for accounts nobody ever asks about again.
    lockout.register_failure("bob")
    clock.advance(901)
    lockout.register_failure("carol")
    assert "bob" not in lockout._states or lockout._states["bob"].failed_count == 1


def test_states_stay_bounded_under_a_flood_of_usernames(clock):
    lockout = AccountLockout(threshold=5, decay_seconds=900, max_entries=50, time_fn=clock)
    for i in range(500):
        lockout.register_failure(f"user{i}")
    assert len(lockout._states) <= 50


def test_eviction_prefers_stale_entries_over_live_lockouts(clock):
    lockout = AccountLockout(
        threshold=2, base_seconds=3600, decay_seconds=900, max_entries=10, time_fn=clock
    )
    for i in range(10):
        lockout.register_failure(f"stale{i}")
    clock.advance(901)  # every one of those aged out

    for _ in range(2):
        lockout.register_failure("victim")  # locked for 3600s
    # A flood of made-up usernames, one failure each (below the lock threshold).
    for i in range(20):
        lockout.register_failure(f"flood{i}")

    assert len(lockout._states) <= 10
    # The live lockout survived; the aged-out entries were the ones shed.
    assert lockout.retry_after("victim") > 0


# --- IpFailureThrottle -------------------------------------------------------

def test_blocks_at_max_failures(clock):
    throttle = IpFailureThrottle(max_failures=3, window_seconds=60, time_fn=clock)
    for _ in range(2):
        throttle.register_failure("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4") is False

    throttle.register_failure("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4") is True
    assert throttle.is_blocked("5.6.7.8") is False


def test_window_rolls_over_and_drops_the_stale_entry(clock):
    throttle = IpFailureThrottle(max_failures=2, window_seconds=60, time_fn=clock)
    throttle.register_failure("1.2.3.4")
    throttle.register_failure("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4") is True

    clock.advance(60)
    assert throttle.is_blocked("1.2.3.4") is False
    assert throttle._states == {}

    # The next failure starts a fresh window rather than resuming the old count.
    throttle.register_failure("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4") is False


def test_success_credits_back_one_failure_not_the_whole_window(clock):
    throttle = IpFailureThrottle(max_failures=3, window_seconds=60, time_fn=clock)
    for _ in range(3):
        throttle.register_failure("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4") is True

    throttle.register_success("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4") is False
    # One good request buys exactly one more guess, not a clean slate.
    throttle.register_failure("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4") is True


def test_success_on_an_untracked_ip_is_a_noop(clock):
    throttle = IpFailureThrottle(time_fn=clock)
    throttle.register_success("1.2.3.4")
    assert throttle._states == {}


def test_states_stay_bounded_under_a_flood_of_ips(clock):
    throttle = IpFailureThrottle(max_failures=3, window_seconds=60, max_entries=50, time_fn=clock)
    for i in range(500):
        throttle.register_failure(f"10.0.0.{i}")
    assert len(throttle._states) <= 50


def test_expired_windows_are_evicted_before_live_ones(clock):
    throttle = IpFailureThrottle(max_failures=3, window_seconds=60, max_entries=10, time_fn=clock)
    for i in range(10):
        throttle.register_failure(f"10.0.0.{i}")
    clock.advance(61)  # all ten windows expired

    for _ in range(3):
        throttle.register_failure("9.9.9.9")  # a live, blocked window
    for i in range(5):
        throttle.register_failure(f"10.1.0.{i}")

    assert len(throttle._states) <= 10
    assert throttle.is_blocked("9.9.9.9") is True
