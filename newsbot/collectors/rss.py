"""RSS/Atom feed collector.

Official-source credibility layer (OpenAI, Anthropic, Unity, etc.). RSS
carries no engagement signals — published_at is the only timestamp. The
hype scorer weights RSS lower than engagement sources (HN/Reddit/GitHub)
and boosts items that also appear on other sources (cross-source bonus).

Config (under news.sources.rss):
  feeds: list[dict]  — each: {name: str, url: str, weight: float}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None

from newsbot.collectors.base import new_candidate, strip_html, truncate, to_iso_utc

log = logging.getLogger(__name__)


async def _fetch_one(feed: dict[str, Any]) -> list[dict[str, Any]]:
    url = str(feed.get("url") or "").strip()
    if not url:
        return []

    source_name = str(feed.get("name") or url).strip()
    # Carry the feed's configured weight into raw_json so the scorer can use it.
    feed_weight = feed.get("weight")
    if feedparser is None:
        log.warning("RSS fetch skipped for %s: feedparser not installed", source_name)
        return []

    try:
        parsed = await asyncio.to_thread(feedparser.parse, url)
    except Exception as exc:
        log.warning("RSS fetch failed for %s url=%s: %s", source_name, url, exc)
        return []

    items: list[dict[str, Any]] = []
    for entry in list(getattr(parsed, "entries", []) or [])[:10]:
        title = str(entry.get("title") or "").strip()
        if not title:
            continue

        summary = entry.get("summary") or entry.get("description") or ""
        # Copy feed weight into raw_json so hype_score() can apply it.
        entry_data = dict(entry)
        if feed_weight is not None:
            entry_data["weight"] = feed_weight
        items.append(
            new_candidate(
                title=title,
                url=str(entry.get("link") or "").strip(),
                source="rss",
                source_name=source_name,
                snippet=truncate(strip_html(str(summary))),
                published_at=to_iso_utc(entry.get("published") or entry.get("updated")),
                raw_text=str(summary).strip() or None,
                raw_json=entry_data,
            )
        )

    if not items:
        log.warning(
            "RSS feed returned zero usable items for %s url=%s status=%s",
            source_name, url, getattr(parsed, "status", "unknown"),
        )
    return items


async def collect(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch RSS candidates. *config* is the news.sources.rss block."""
    feeds = config.get("feeds") or []
    if not feeds:
        return []
    batches = await asyncio.gather(*[_fetch_one(f) for f in feeds if isinstance(f, dict)])
    return [item for batch in batches for item in batch]