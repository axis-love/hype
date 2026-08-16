"""Pure temperature-gated pick logic for the hype store.

Deliberately dependency-free: no db, no Telegram, no env reads. Every knob
(threshold floor, ratio, merge bonus/cap) is a parameter, so any consumer —
the Telegram poster today, girllm hot_take and the blog writer later — can
import and reuse it unchanged.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from newsbot.scoring import current_temperature, merge_multiplier


@dataclass
class PickResult:
    """Outcome of one pick_hottest call."""

    row: dict[str, Any] | None  # chosen store row, or None
    reason: str  # "picked" | "empty" | "below_threshold"
    threshold: float
    median: float
    hottest: float
    temps: dict[int, float]  # row_id -> raw current temp (reusable for eviction / /scores)


def pick_hottest(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    now: datetime,
    floor: float,
    ratio: float,
    merge_bonus: float,
    merge_cap: float,
) -> PickResult:
    """Pick the hottest eligible store row for posting.

    temps = current_temperature per row; threshold = max(floor, ratio *
    median(temps)); eligible = raw temp >= threshold; winner = max of
    eligible by raw_temp * merge_multiplier. The merge multiplier affects
    RANKING only — it never makes a below-threshold row eligible.
    """
    temps = {row["id"]: current_temperature(row, config, now=now) for row in rows}
    if not rows:
        return PickResult(row=None, reason="empty", threshold=0.0, median=0.0, hottest=0.0, temps=temps)

    median = statistics.median(temps.values())
    hottest = max(temps.values())
    threshold = max(floor, ratio * median)

    eligible = [row for row in rows if temps[row["id"]] >= threshold]
    if not eligible:
        return PickResult(row=None, reason="below_threshold", threshold=threshold, median=median, hottest=hottest, temps=temps)

    winner = max(
        eligible,
        key=lambda row: temps[row["id"]] * merge_multiplier(row.get("merge_count"), bonus=merge_bonus, cap=merge_cap),
    )
    return PickResult(row=winner, reason="picked", threshold=threshold, median=median, hottest=hottest, temps=temps)
