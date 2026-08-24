"""Local wall-clock helpers for slot-based scheduling.

The schedule runs on local wall-clock time, not UTC intervals. Timezone is
read from the ``NEWS_TZ`` environment variable on every call (default
``Asia/Bangkok``), so operators can re-anchor the schedule without code
changes. The pip ``tzdata`` package is a dependency: the slim Docker image
has no system zoneinfo database, and ``ZoneInfo`` raises
``ZoneInfoNotFoundError`` without it.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Bangkok"

#: Default generation hours (local wall-clock). The H-6 cadence — every 4 h
#: from 05:00. Declared once here so main.py, README, .env.example, and
#: compose.yml all reference the same value without hand-syncing.
DEFAULT_GEN_HOURS = "5,9,13,17,21"


def local_now() -> datetime:
    """Current wall-clock time in ``NEWS_TZ`` (default Asia/Bangkok)."""
    return datetime.now(ZoneInfo(os.getenv("NEWS_TZ", DEFAULT_TZ)))


def gen_slots(env_str: str) -> list[int]:
    """Parse ``NEWS_GEN_HOURS`` (e.g. ``"5,17"``) into hour ints.

    Raises ValueError on non-integer tokens or hours outside 0-23.
    """
    slots: list[int] = []
    for token in env_str.split(","):
        token = token.strip()
        try:
            hour = int(token)
        except ValueError:
            raise ValueError(f"invalid gen hour: {token!r}") from None
        if not 0 <= hour <= 23:
            raise ValueError(f"gen hour out of range 0-23: {hour}")
        slots.append(hour)
    return slots


def post_slot(dt: datetime) -> str | None:
    """Slot key ``YYYY-MM-DDTHH`` for a post slot, or None on odd hours."""
    if dt.hour % 2 != 0:
        return None
    return dt.strftime("%Y-%m-%dT%H")


def summary_day(dt: datetime) -> str:
    """Day key ``YYYY-MM-DD`` for the daily summary slot."""
    return dt.strftime("%Y-%m-%d")


def latest_due_gen_slot(dt: datetime, hours: list[int]) -> str:
    """Most recent scheduled gen slot at or before ``dt``.

    Rolls back to the previous day's latest hour when ``dt`` precedes
    today's first scheduled hour — a missed digest must still be due.
    """
    for offset in (0, 1):
        day = dt - timedelta(days=offset)
        candidates = [h for h in hours if h <= day.hour] if offset == 0 else hours
        if candidates:
            return f"{day.strftime('%Y-%m-%d')}T{max(candidates):02d}"
    raise ValueError("hours must not be empty")
