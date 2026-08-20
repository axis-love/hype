"""Shared helpers for v2 store/posting tests.

scored_story() builds a candidate whose persisted score_breakdown yields a
known, deterministic temperature (≈ engagement for fresh rows). echo_style()
is an async styler stub shaped like newsbot.summarizer.llm_style_posts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def scored_story(title: str, engagement: float, *, hours_old: float = 1.0) -> dict[str, Any]:
    """Story dict whose score_breakdown persists a known engagement score.

    With fresh recency (~1.0), weight 1.0, no bonuses, penalty 1.0 the
    row's current temperature is ≈ engagement * recency.

    published_at is relative to the REAL clock: the production temperature
    recalc uses wall-clock now, so a frozen date here would decay the rows
    a little more every real day until they fall below the posting
    threshold and the tests rot. Tests that need exact temperatures freeze
    newsbot.jobs.datetime instead (see test_posting_gate).
    """
    now = datetime.now(timezone.utc)
    published = (now - timedelta(hours=hours_old)).isoformat(timespec="seconds")
    return {
        "title": title,
        "url": f"https://example.com/{title.lower().replace(' ', '-')}",
        "source": "hn",
        "source_name": "Hacker News",
        "snippet": f"Snippet for {title}",
        "upvotes": int(engagement),
        "comments": 1,
        "published_at": published,
        "score_breakdown": {
            "source": "hn",
            "published_at": published,
            "upvotes": int(engagement),
            "comments": 1,
            "stars": 0,
            "reposts": 0,
            "crosspost_count": 1,
            "penalty": 1.0,
            "lookback_hours": 48,
            "score": engagement,
            "engagement": engagement,
            "recency": 1.0,
            "source_weight": 1.0,
            "topic_bonus": 0,
            "crosspost_bonus": 0.0,
            "matched_topics": [],
            "scored_at": published,
        },
    }


async def echo_style(items: list, lm: Any, **kw: Any) -> list[dict[str, Any]]:
    """Styler stub: one styled dict per input, original title preserved."""
    return [
        {"title": it.get("title") or "S", "body": f"Body for {it.get('title')}"}
        for it in items
    ]
