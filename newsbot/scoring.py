"""Hype scoring.

Implements the architecture spec §8:

    hype_score = engagement * recency * source_weight + topic_bonus + crosspost_bonus

  engagement     = log1p(upvotes)*10 + log1p(comments)*25
                 + log1p(stars)*15   + log1p(reposts)*20
  recency        = exp(-age_hours / lookback_hours)  (exponential decay)
  source_weight  = SOURCE_WEIGHTS[source] (default 1.0)
  topic_bonus    = sum of boosts for topics matched in title/snippet
  crosspost_bonus= 30 if the item appeared on >=2 sources (stamped by dedupe)

The code discovers hype; the LLM only writes the digest.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from newsbot.config import TOPIC_KEYWORDS


def recency_decay(published_at: Any, *, lookback_hours: float) -> float:
    """Exponential decay: 1.0 now, ~0.37 at lookback_hours, ~0.07 at 2*lookback.

    Items without a parseable published_at get a neutral 0.5 (neither fresh
    nor stale) so they don't dominate but aren't buried either.
    """
    if not published_at:
        return 0.5

    if isinstance(published_at, datetime):
        dt = published_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        s = str(published_at).strip()
        if not s:
            return 0.5
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            # Try epoch seconds.
            try:
                dt = datetime.fromtimestamp(float(s), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return 0.5

    age_hours = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    if lookback_hours <= 0:
        return 0.5
    return math.exp(-age_hours / lookback_hours)


def topic_bonus(item: dict[str, Any], topic_boost: dict[str, int]) -> int:
    """Sum boosts for topics whose keywords appear in title or snippet."""
    haystack = " ".join(
        s for s in (str(item.get("title") or ""), str(item.get("snippet") or ""),
                    str(item.get("raw_text") or "")) if s
    ).lower()
    if not haystack:
        return 0

    total = 0
    for boost_key, keywords in TOPIC_KEYWORDS.items():
        weight = topic_boost.get(boost_key, 0)
        if weight <= 0:
            continue
        if any(kw in haystack for kw in keywords):
            total += weight
    return total


def engagement(item: dict[str, Any]) -> float:
    """log1p-weighted engagement from upvotes/comments/stars/reposts."""
    upvotes = item.get("upvotes") or 0
    comments = item.get("comments") or 0
    stars = item.get("stars") or 0
    reposts = item.get("reposts") or 0
    return (
        math.log1p(max(0, upvotes)) * 10.0
        + math.log1p(max(0, comments)) * 25.0
        + math.log1p(max(0, stars)) * 15.0
        + math.log1p(max(0, reposts)) * 20.0
    )


def hype_score(item: dict[str, Any], config: dict[str, Any]) -> float:
    """Compute the hype score for one candidate."""
    source_weights: dict[str, float] = config.get("source_weights") or {}
    topic_boost: dict[str, int] = config.get("topic_boost") or {}

    src = str(item.get("source") or "").strip()
    weight = float(source_weights.get(src, 1.0))

    # Official-RSS override: if the candidate carries a per-feed weight
    # (RSS feeds in config can set 'weight'), use max(global, feed) so an
    # official blog (1.3) outranks a normal RSS feed (0.5).
    raw_json = item.get("raw_json")
    if isinstance(raw_json, dict):
        feed_weight = raw_json.get("weight")
        if feed_weight:
            try:
                weight = max(weight, float(feed_weight))
            except (TypeError, ValueError):
                pass

    eng = engagement(item)
    rec = recency_decay(item.get("published_at"), lookback_hours=float(config.get("lookback_hours") or 48))
    topics = topic_bonus(item, topic_boost)

    crosspost = 0.0
    cp_count = int(item.get("crosspost_count") or 1)
    if cp_count >= 2:
        crosspost = 30.0

    penalty = float(item.get("penalty") or 1.0)

    return (eng * rec * weight + topics + crosspost) * penalty


def score_all(items: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Stamp each item with a 'score' field and return them."""
    for item in items:
        item["score"] = hype_score(item, config)
    return items