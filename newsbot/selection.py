"""Pure selection logic for the hype pipeline.

Deliberately dependency-free: no db, no Telegram, no env reads. Every knob
(threshold floor, ratio, merge bonus/cap, source quota) is a parameter, so
any consumer — the Telegram poster today, the score replay tool, girllm
hot_take and the blog writer later — can import and reuse it unchanged.
"""
from __future__ import annotations

import json
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
    excluded_ids: frozenset[int] = frozenset()  # rows removed from the eligible set before pick (e.g. same-topic cooldown)


def pick_hottest(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    now: datetime,
    floor: float,
    ratio: float,
    merge_bonus: float,
    merge_cap: float,
    excluded_ids: set[int] | None = None,
) -> PickResult:
    """Pick the hottest eligible store row for posting.

    temps = current_temperature per row; threshold = max(floor, ratio *
    median(temps)); eligible = raw temp >= threshold AND row id NOT in
    excluded_ids; winner = max of eligible by raw_temp * merge_multiplier.
    The merge multiplier affects RANKING only — it never makes a
    below-threshold row eligible.

    Excluded rows still participate in temps/median (threshold stays
    comparable across slots) but are removed from the ELIGIBLE set.
    If everything eligible is excluded, the result reason is
    "below_threshold" (PickResult fields stay stable). The caller
    computes the exclusion set — selection.py stays dependency-free.
    """
    temps = {row["id"]: current_temperature(row, config, now=now) for row in rows}
    excl = frozenset(excluded_ids or ())
    if not rows:
        return PickResult(row=None, reason="empty", threshold=0.0, median=0.0, hottest=0.0, temps=temps, excluded_ids=excl)

    median = statistics.median(temps.values())
    hottest = max(temps.values())
    threshold = max(floor, ratio * median)

    eligible = [
        row for row in rows
        if temps[row["id"]] >= threshold
        and (excluded_ids is None or row["id"] not in excluded_ids)
    ]
    if not eligible:
        return PickResult(row=None, reason="below_threshold", threshold=threshold, median=median, hottest=hottest, temps=temps, excluded_ids=excl)

    winner = max(
        eligible,
        key=lambda row: temps[row["id"]] * merge_multiplier(row.get("merge_count"), bonus=merge_bonus, cap=merge_cap),
    )
    return PickResult(row=winner, reason="picked", threshold=threshold, median=median, hottest=hottest, temps=temps, excluded_ids=excl)


def select_for_consumer(
    rows: list[dict[str, Any]],
    deliveries_for_channel: list[dict[str, Any]],
    profile: dict[str, Any],
    config: dict[str, Any],
    *,
    now: datetime,
) -> PickResult:
    """Per-consumer selection: topic filter → cooldown → pick_hottest.

    Design note §3 (consumer profiles) and §4 (per-consumer median).

    1. Topic filter: if profile['topics'] is not None, keep only rows
       whose origin_topic OR matched_topics intersects the profile's
       topic list. Rows with NULL/empty origin_topic are kept only if
       their matched_topics intersect (or if topics is None = all).

    2. Per-consumer cooldown: count deliveries per origin_topic in the
       consumer's own deliveries_for_channel list (already scoped to
       this channel by the caller). Rows whose topic has >=
       profile['cooldown_max'] recent deliveries are excluded. Rows
       with NULL/empty origin_topic are never excluded.

    3. pick_hottest over the filtered rows only — median is per-consumer
       (§4), so a science-heavy store doesn't inflate the gaming median.

    No styling — consumers own voice (decision 2026-09-03).
    """
    topics = profile.get("topics")
    # 1. Topic filter.
    if topics is not None:
        topic_set = set(topics)
        filtered: list[dict[str, Any]] = []
        for row in rows:
            origin = str(row.get("origin_topic") or "").strip()
            matched = row.get("matched_topics")
            if isinstance(matched, str):
                try:
                    matched = json.loads(matched)
                except (ValueError, TypeError):
                    matched = []
            matched_list = set(matched or [])
            if origin in topic_set or matched_list & topic_set:
                filtered.append(row)
        rows = filtered

    # 2. Per-consumer cooldown.
    cooldown_max = int(profile.get("cooldown_max", 0))
    excluded_ids: set[int] = set()
    if cooldown_max > 0 and rows:
        topic_counts: dict[str, int] = {}
        for p in deliveries_for_channel:
            topic = str(p.get("origin_topic") or "").strip()
            if topic:
                topic_counts[topic] = topic_counts.get(topic, 0) + 1
        for row in rows:
            topic = str(row.get("origin_topic") or "").strip()
            if topic and topic_counts.get(topic, 0) >= cooldown_max:
                excluded_ids.add(row["id"])

    # 3. pick_hottest over the filtered rows only.
    return pick_hottest(
        rows, config, now=now,
        floor=float(profile.get("floor", 35.0)),
        ratio=float(profile.get("ratio", 0.5)),
        merge_bonus=float(profile.get("merge_bonus", 0.2)),
        merge_cap=float(profile.get("merge_cap", 2.0)),
        excluded_ids=excluded_ids or None,
    )


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
