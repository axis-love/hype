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
import re
from datetime import datetime, timezone
from typing import Any

from newsbot.config import TOPIC_KEYWORDS, _SOURCE_ALIASES

# Pre-compiled regex patterns for short keywords that need word-boundary matching.
# Short keywords (≤3 chars or single-word) get \b boundaries to prevent
# matching substrings like "ai" in "email". Multi-word phrases use substring.
_SHORT_KEYWORD_PATTERNS: dict[str, list[tuple[re.Pattern, str]]] = {}


def _build_keyword_patterns() -> None:
    """Pre-compile keyword patterns for boundary-aware matching.

    Single-word keywords use word-boundary regex to prevent false positives
    like "ai" matching "email", "unity" matching "community".
    Multi-word phrases use substring matching (they are already specific enough).
    """
    for boost_key, keywords in TOPIC_KEYWORDS.items():
        patterns: list[tuple[re.Pattern, str]] = []
        for kw in keywords:
            kw_lower = kw.lower()
            if " " in kw:
                # Multi-word phrase: use substring match.
                patterns.append((None, kw_lower))
            else:
                # Single word: use word-boundary regex to prevent
                # matching substrings (unity -> community, ai -> email).
                patterns.append((re.compile(r"\b" + re.escape(kw_lower) + r"\b"), kw_lower))
        _SHORT_KEYWORD_PATTERNS[boost_key] = patterns


_build_keyword_patterns()


def _validate_now(now: datetime | None) -> datetime:
    """Validate and normalize the 'now' parameter for deterministic scoring.

    Returns a timezone-aware UTC datetime. Raises ValueError if now is
    provided but naive (no timezone info).
    """
    if now is None:
        return datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError(
            "now must be timezone-aware (got naive datetime). "
            "Use datetime.now(timezone.utc) or pass an aware datetime."
        )
    if now.tzinfo is not timezone.utc:
        return now.astimezone(timezone.utc)
    return now


def recency_decay(
    published_at: Any,
    *,
    lookback_hours: float,
    now: datetime | None = None,
) -> float:
    """Exponential decay: 1.0 now, ~0.37 at lookback_hours, ~0.135 at 2*lookback.

    Items without a parseable published_at get a neutral 0.5 (neither fresh
    nor stale) so they don't dominate but aren't buried either.

    Pass ``now`` for deterministic scoring — all items in a batch should
    share the same timestamp.
    """
    effective_now = _validate_now(now)

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

    age_hours = max(0.0, (effective_now - dt).total_seconds() / 3600.0)
    if lookback_hours <= 0:
        return 0.5
    return math.exp(-age_hours / lookback_hours)


def _topic_bonus_with_matches(
    item: dict[str, Any], topic_boost: dict[str, int]
) -> tuple[int, list[str]]:
    """Compute topic bonus and matched topic keys in a single pass.

    Internal helper used by score_breakdown(). Public topic_bonus() wraps
    this to preserve its int-only API.
    """
    haystack = " ".join(
        s for s in (str(item.get("title") or ""), str(item.get("snippet") or ""),
                    str(item.get("raw_text") or "")) if s
    ).lower()
    if not haystack:
        return 0, []

    total = 0
    matched: list[str] = []
    for boost_key, patterns in _SHORT_KEYWORD_PATTERNS.items():
        weight = topic_boost.get(boost_key, 0)
        if weight <= 0:
            continue
        for regex, kw_lower in patterns:
            if regex is not None:
                # Short keyword: word-boundary match.
                if regex.search(haystack):
                    total += weight
                    matched.append(boost_key)
                    break
            else:
                # Multi-word phrase or long keyword: substring match.
                if kw_lower in haystack:
                    total += weight
                    matched.append(boost_key)
                    break
    return total, matched


def topic_bonus(item: dict[str, Any], topic_boost: dict[str, int]) -> int:
    """Sum boosts for topics whose keywords appear in title or snippet.

    Short keywords (≤4 chars, single word) use word-boundary matching to
    prevent false positives like "ai" matching "email". Multi-word phrases
    and longer keywords use substring matching.

    Returns the total bonus (int).
    """
    bonus, _ = _topic_bonus_with_matches(item, topic_boost)
    return bonus


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


def score_breakdown(
    item: dict[str, Any],
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute the hype score and return all components as a dict.

    This is the shared contract for persistence (Task 2), logging (Task 4),
    and the /scores command (Task 3). All components are computed in a
    single pass — topic_bonus and matched_topics never drift apart.

    Returns dict with keys:
        score, engagement, recency, source_weight, topic_bonus,
        crosspost_bonus, penalty, matched_topics, scored_at
    """
    source_weights: dict[str, float] = config.get("source_weights") or {}
    topic_boost: dict[str, int] = config.get("topic_boost") or {}

    src = str(item.get("source") or "").strip()
    src = _SOURCE_ALIASES.get(src, src)
    weight = float(source_weights.get(src, 1.0))

    # Official-RSS override: if the candidate carries a per-feed weight
    # (RSS feeds in config can set 'weight'), use the feed weight directly,
    # replacing the global source weight.
    raw_json = item.get("raw_json")
    if isinstance(raw_json, dict):
        feed_weight = raw_json.get("weight")
        if feed_weight is not None:
            try:
                weight = float(feed_weight)
            except (TypeError, ValueError):
                pass

    effective_now = _validate_now(now)
    lookback_hours = float(config.get("lookback_hours") or 48)

    eng = engagement(item)
    rec = recency_decay(item.get("published_at"), lookback_hours=lookback_hours, now=effective_now)
    topics, matched = _topic_bonus_with_matches(item, topic_boost)

    crosspost = 0.0
    cp_count = int(item.get("crosspost_count") or 1)
    if cp_count >= 2:
        crosspost = 30.0

    raw_penalty = item.get("penalty")
    penalty = float(raw_penalty) if raw_penalty is not None else 1.0

    score = (eng * rec * weight + topics + crosspost) * penalty

    return {
        "score": score,
        "engagement": eng,
        "recency": rec,
        "source_weight": weight,
        "topic_bonus": topics,
        "crosspost_bonus": crosspost,
        "penalty": penalty,
        "matched_topics": matched,
        "scored_at": effective_now.isoformat(timespec="seconds"),
    }


def hype_score(
    item: dict[str, Any],
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> float:
    """Compute the hype score for one candidate.

    Pass ``now`` for deterministic scoring — all items in a batch should
    share the same timestamp.
    """
    return score_breakdown(item, config, now=now)["score"]


def score_all(
    items: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Stamp each item with 'score' and 'score_breakdown' fields.

    When ``now`` is provided, all items share the same timestamp for
    deterministic scoring. When omitted, one timestamp is captured at
    the start of the batch and reused for all items.
    """
    effective_now = _validate_now(now)
    for item in items:
        bd = score_breakdown(item, config, now=effective_now)
        item["score"] = bd["score"]
        item["score_breakdown"] = bd
    return items