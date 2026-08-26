"""What a user has spent on chat today, and whether they may spend more.

Separate from ``budget.py`` deliberately: that module is pure arithmetic over
strings and token counts and has no database imports, which is what makes it
trivially testable. This one is a query.

The cap is per-user and per-UTC-day. It complements rather than duplicates the
per-IP rate limit on the endpoint: a rate limit bounds *bursts*, this bounds the
*bill*. Both are needed — twenty questions a minute is fine until it is the
thousandth question of the day against a metered provider.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import ChatConversation, ChatMessage


class DailyCapExceeded(Exception):
    """Raised when a user has already spent their allowance for the UTC day.

    Carries the numbers so the router can tell the user how much is left and when
    it resets, rather than an opaque "quota exceeded".
    """

    def __init__(self, spent: float, limit: float):
        self.spent = spent
        self.limit = limit
        super().__init__(
            f"Daily chat budget reached: ${spent:.4f} of ${limit:.2f} used. "
            f"It resets at 00:00 UTC."
        )


def _utc_day_start(now: datetime | None = None) -> datetime:
    """Midnight UTC for the current day.

    A fixed UTC boundary rather than a rolling 24-hour window: it is cheap to
    query, and "resets at midnight UTC" is something a user can be told and can
    predict. Stored timestamps are naive-UTC (``models.utcnow``), so the tzinfo
    is stripped to compare against the column.
    """
    now = now or datetime.now(timezone.utc)
    start = now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start.replace(tzinfo=None)


def spent_today(db: Session, user_id: int, *, now: datetime | None = None) -> float:
    """Total USD this user's chat turns have cost since midnight UTC.

    Joined through the conversation because cost lives on the message and
    ownership lives on the thread — the same reason a message has no ``user_id``
    of its own to drift out of sync with its parent.
    """
    total = (
        db.query(func.coalesce(func.sum(ChatMessage.cost_usd), 0.0))
        .join(ChatConversation, ChatMessage.conversation_id == ChatConversation.id)
        .filter(
            ChatConversation.user_id == user_id,
            ChatMessage.created_at >= _utc_day_start(now),
        )
        .scalar()
    )
    return float(total or 0.0)


def next_reset(now: datetime | None = None) -> datetime:
    """When the current day's allowance resets, as an aware UTC datetime."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return (now.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1))


def check_daily_cap(db: Session, user_id: int, limit: float) -> float:
    """Raise :class:`DailyCapExceeded` if the user is out of budget; else return
    what they have spent so far.

    Checked *before* the request rather than after, so the cap is a gate and not
    merely a report. That means a single turn can carry the total slightly past
    the limit — the alternative is refusing to start a turn whose cost is not yet
    knowable, which would make the last few cents of any budget unusable.
    """
    if limit <= 0:
        return spent_today(db, user_id)  # 0 or negative ⇒ no cap configured
    spent = spent_today(db, user_id)
    if spent >= limit:
        raise DailyCapExceeded(spent, limit)
    return spent
