"""Pure selection logic for the hype pipeline.

Deliberately dependency-free: no db, no Telegram, no env reads. Every knob
(threshold floor, ratio, merge bonus/cap, source quota) is a parameter, so
any consumer — the Telegram poster today, the score replay tool, girllm
hot_take and the blog writer later — can import and reuse it unchanged.
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


def select_diverse_candidates(
    scored: list[dict[str, Any]],
    max_candidates: int,
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Select top candidates with guaranteed source diversity.

    Uses round-robin allocation: sources are ordered by their top score,
    and each source contributes one item per round until it exhausts its
    quota or all slots are filled. This ensures every source with eligible
    candidates gets at least one slot before any source gets a second.

    When a guarantee cannot be met (not enough eligible items), remaining
    slots are filled by global score ranking.
    """
    if not scored:
        return []

    sq = cfg.get("source_quota")
    source_quota = int(sq) if sq is not None else 8

    # Deterministic sort key: score desc, title asc, source asc, URL asc.
    # Used everywhere to ensure order-independent selection.
    def _sort_key(c: dict[str, Any]) -> tuple:
        return (
            -float(c.get("score") or 0.0),
            str(c.get("title") or ""),
            str(c.get("source") or ""),
            str(c.get("url") or ""),
        )

    # Group by source, sorted by score within each group.
    by_source: dict[str, list[dict[str, Any]]] = {}
    for c in scored:
        src = str(c.get("source") or "unknown")
        by_source.setdefault(src, []).append(c)
    for src in by_source:
        by_source[src].sort(key=_sort_key)

    # Order sources by their top item's score (descending), then alphabetically.
    # Uses the same key (including title) as the pool sort for consistency.
    source_order = sorted(
        by_source,
        key=lambda s: (
            -float(by_source[s][0].get("score") or 0.0),
            str(by_source[s][0].get("title") or ""),
            s,
        ),
    )

    top: list[dict[str, Any]] = []
    used: set[int] = set()

    # Phase 1: round-robin allocation — one item per source per round.
    # This ensures every source gets at least one slot before any gets two.
    rounds = min(source_quota, max_candidates)
    for round_idx in range(rounds):
        for src in source_order:
            if len(top) >= max_candidates:
                break
            items = by_source[src]
            if round_idx < len(items):
                item = items[round_idx]
                if id(item) not in used:
                    top.append(item)
                    used.add(id(item))
        if len(top) >= max_candidates:
            break

    # Phase 2: fill remaining slots by global score ranking.
    # Use the same deterministic key: score desc, title asc, source asc, URL asc.
    if len(top) < max_candidates:
        remaining = [c for c in scored if id(c) not in used]
        remaining.sort(key=_sort_key)
        for item in remaining:
            top.append(item)
            if len(top) >= max_candidates:
                break

    # Re-sort the final selection by score for the LLM filter.
    # Deterministic tie-break: score desc, title asc, source asc, URL asc.
    top.sort(key=_sort_key)
    return top
