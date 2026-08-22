"""Google Trends collector.

Polls https://trends.google.com/trending/rss?geo=US (geo configurable,
list of geos). Each trending story produces up to 3 Candidates — one per
related news link: title = news headline, url = news link,
source = "trends", source_name = "trends/<topic title>",
reposts = traffic mapped (200+ → 200, 1000+ → 1000, …, Breakout → 5000).

The existing dedupe merges trends candidates with matching articles from
IGN/Reddit via canonical URL or title; the crosspost bonus fires. One new
dedupe rule, scoped to source == "trends", merges a trends candidate with
any candidate whose title contains ALL of the trend title's tokens (minus
stopwords, ≥2 tokens). Logged as "dedupe_trends_match".

Config (under news.sources.trends):
  geos: list[str]   — e.g. ["US", "GB"]. Default ["US"].
  limit: int        — max news items per trend (default 3, capped at 3).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None

from newsbot.collectors.base import Candidate, new_candidate, strip_html, truncate, to_iso_utc
from newsbot.collectors._shared import get_shared_semaphore

log = logging.getLogger(__name__)

_TRENDS_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

_TRENDS_RSS_BASE = "https://trends.google.com/trending/rss"

# Max news links per trending topic (plan says "cap 3").
_MAX_NEWS_PER_TREND = 3

# Traffic string → reposts mapping. Breakout is the highest signal.
# The plan: 200+→200, 1000+→1000, ..., Breakout→5000.
_TRAFFIC_MAP: dict[str, int] = {
    "200+": 200,
    "500+": 500,
    "1000+": 1000,
    "2000+": 2000,
    "5000+": 5000,
    "10000+": 10000,
    "50000+": 50000,
    "100000+": 100000,
    "500000+": 500000,
    "1000000+": 1000000,
    "breakout": 5000,
}


def _traffic_to_reposts(traffic: str | None) -> int:
    """Map a Google Trends traffic string to a reposts integer.

    Values like "200+", "1000+", "Breakout" are mapped. Unknown or
    missing values yield 0.
    """
    if not traffic:
        return 0
    key = traffic.strip().lower()
    if key in _TRAFFIC_MAP:
        return _TRAFFIC_MAP[key]
    # Try parsing a bare number (e.g. "1500").
    try:
        return int(float(key))
    except (ValueError, TypeError):
        return 0


def _extract_news_items(entry: Any) -> list[dict[str, str]]:
    """Extract news items from a feedparser entry.

    feedparser returns ht_news_item_title as a str (single item) or
    list[str] (multiple items). Same for ht_news_item_url and
    ht_news_item_source.
    """
    titles = entry.get("ht_news_item_title")
    urls = entry.get("ht_news_item_url")
    sources = entry.get("ht_news_item_source")

    # Normalize to lists.
    if isinstance(titles, str):
        titles = [titles]
    if isinstance(urls, str):
        urls = [urls]
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(titles, list):
        titles = []
    if not isinstance(urls, list):
        urls = []
    if not isinstance(sources, list):
        sources = []

    items: list[dict[str, str]] = []
    count = min(len(titles), len(urls), _MAX_NEWS_PER_TREND)
    for i in range(count):
        title = str(titles[i]).strip()
        url = str(urls[i]).strip()
        if title and url and url.startswith("http"):
            source = str(sources[i]).strip() if i < len(sources) else ""
            items.append({"title": title, "url": url, "source": source})
    return items


async def _fetch_one_geo(geo: str, limit: int) -> list[Candidate]:
    """Fetch trending RSS for one geo and return candidates."""
    url = f"{_TRENDS_RSS_BASE}?geo={geo}"

    if feedparser is None:
        log.warning("Trends fetch skipped: feedparser not installed")
        return []

    sem = get_shared_semaphore()
    async with sem:
        try:
            async with httpx.AsyncClient(
                timeout=_TRENDS_TIMEOUT, follow_redirects=True,
            ) as client:
                response = await client.get(url)
                content = response.content
        except httpx.TimeoutException:
            log.warning("Trends fetch timed out for geo=%s url=%s", geo, url)
            return []
        except Exception as exc:
            log.warning("Trends fetch failed for geo=%s url=%s: %s", geo, url, exc)
            return []

    try:
        parsed = feedparser.parse(content)
    except Exception as exc:
        log.warning("Trends parse failed for geo=%s url=%s: %s", geo, url, exc)
        return []

    items: list[Candidate] = []
    for entry in list(getattr(parsed, "entries", []) or []):
        trend_title = str(entry.get("title") or "").strip()
        if not trend_title:
            continue

        traffic = entry.get("ht_approx_traffic")
        reposts = _traffic_to_reposts(traffic)
        published = entry.get("published") or entry.get("updated")

        news_items = _extract_news_items(entry)
        if not news_items:
            continue

        for news in news_items[:limit]:
            items.append(
                new_candidate(
                    title=news["title"],
                    url=news["url"],
                    source="trends",
                    source_name=f"trends/{trend_title}",
                    snippet=news.get("source") or "",
                    published_at=to_iso_utc(published),
                    reposts=reposts,
                    raw_json={
                        "trend_title": trend_title,
                        "traffic": traffic,
                        "news_source": news.get("source"),
                        "geo": geo,
                    },
                )
            )

    if not items:
        log.warning("Trends feed returned zero usable items for geo=%s url=%s", geo, url)
    return items


async def collect(config: dict[str, Any]) -> list[Candidate]:
    """Fetch Google Trends candidates.

    *config* is the news.sources.trends block. Geos are fetched
    sequentially (Google Trends RSS is not rate-limit-tolerant).
    Never raises: any failure logs a warning and returns [].
    """
    geos_raw = config.get("geos")
    if geos_raw is None:
        geos_raw = ["US"]
    geos: list[str] = []
    for raw in geos_raw:
        geo = str(raw).strip()
        if geo and geo not in geos:
            geos.append(geo)

    limit = max(1, min(int(config.get("limit") or _MAX_NEWS_PER_TREND), _MAX_NEWS_PER_TREND))

    results: list[Candidate] = []
    for geo in geos:
        try:
            results.extend(await _fetch_one_geo(geo, limit))
        except Exception as exc:  # defensive: the collector never raises
            log.warning("Trends fetch error for geo=%s: %s", geo, exc)
    return results
