"""Trend source fetchers for Natalie."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import html
import logging
import re
from typing import Any

import httpx

try:
    import feedparser
except ImportError:  # pragma: no cover - exercised via runtime warning path
    feedparser = None


log = logging.getLogger(__name__)

REDDIT_USER_AGENT = "girllm:trends:v1 (by /u/girllm)"
YOUTUBE_TRENDING_API_URL = "https://inv.nadeko.net/api/v1/trending"
YOUTUBE_TRENDING_RSS_URL = "https://www.youtube.com/feeds/videos.xml?chart=trending"
HN_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"
DEVTO_API_URL = "https://dev.to/api/articles"
LOBSTERS_API_URL = "https://lobste.rs/hottest.json"


def _as_source_dict(source: Any, *, source_type: str) -> dict[str, Any] | None:
    if isinstance(source, dict):
        return source
    log.warning("%s trend source is invalid, expected a mapping: %r", source_type, source)
    return None


def _strip_html(text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", text or "")
    return html.unescape(re.sub(r"\s+", " ", no_tags)).strip()


def _truncate(text: str, limit: int = 200) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


async def _fetch_single_rss(source: dict[str, Any]) -> list[dict[str, Any]]:
    source_dict = _as_source_dict(source, source_type="RSS")
    if source_dict is None:
        return []

    url = str(source_dict.get("url") or "").strip()
    if not url:
        return []

    source_name = str(source_dict.get("name") or url).strip()
    if feedparser is None:
        log.warning(
            "RSS fetch failed for %s url=%s status=unavailable: feedparser is not installed",
            source_name,
            url,
        )
        return []

    try:
        parsed = await asyncio.to_thread(feedparser.parse, url)
    except Exception as exc:
        log.warning("RSS fetch failed for %s url=%s status=unavailable: %s", source_name, url, exc)
        return []

    items: list[dict[str, Any]] = []
    for entry in list(getattr(parsed, "entries", []) or [])[:10]:
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        summary = entry.get("summary") or entry.get("description") or ""
        items.append(
            {
                "source": "rss",
                "source_name": source_name,
                "title": title,
                "snippet": _truncate(_strip_html(str(summary))),
                "url": str(entry.get("link") or "").strip(),
            }
        )

    if not items:
        log.warning(
            "RSS feed returned zero usable items for %s url=%s status=%s",
            source_name,
            url,
            getattr(parsed, "status", "unknown"),
        )

    return items


async def _fetch_subreddit_rss(
    *,
    subreddit: str,
    source_name: str,
    limit: int,
) -> list[dict[str, Any]]:
    url = f"https://www.reddit.com/r/{subreddit}/hot/.rss?limit={limit}"
    items = await _fetch_single_rss({"url": url, "name": source_name})
    for item in items:
        item["source"] = "reddit"
    return items


async def fetch_rss(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch trend items from RSS/Atom feeds."""
    if not sources:
        return []
    batches = await asyncio.gather(*[_fetch_single_rss(source) for source in sources])
    return [item for batch in batches for item in batch]


async def _fetch_single_subreddit(
    client: httpx.AsyncClient,
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    source_dict = _as_source_dict(source, source_type="Reddit")
    if source_dict is None:
        return []

    source_name = "reddit"
    url = ""

    try:
        subreddit = str(source_dict.get("subreddit") or "").strip().strip("/")
        if not subreddit:
            return []

        limit = max(1, min(int(source_dict.get("limit") or 10), 25))
        url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}"
        source_name = f"r/{subreddit}"
        response = await client.get(url)
        if response.status_code >= 400:
            log.warning(
                "Reddit fetch failed for %s url=%s status=%s; trying RSS fallback",
                source_name,
                url,
                response.status_code,
            )
            return await _fetch_subreddit_rss(
                subreddit=subreddit,
                source_name=source_name,
                limit=limit,
            )
        payload = response.json()
    except Exception as exc:
        log.warning("Reddit fetch failed for %s url=%s status=unavailable: %s", source_name, url, exc)
        if url:
            return await _fetch_subreddit_rss(
                subreddit=subreddit,
                source_name=source_name,
                limit=limit,
            )
        return []

    items: list[dict[str, Any]] = []
    children = ((payload or {}).get("data") or {}).get("children") or []
    for child in children:
        data = child.get("data") or {}
        if data.get("stickied"):
            continue

        title = str(data.get("title") or "").strip()
        if not title:
            continue

        permalink = str(data.get("permalink") or "").strip()
        full_url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
        items.append(
            {
                "source": "reddit",
                "source_name": source_name,
                "title": title,
                "snippet": _truncate(str(data.get("selftext") or "").strip()),
                "url": full_url,
            }
        )

    if items:
        return items
    log.warning("Reddit fetch returned zero usable items for %s url=%s status=%s; trying RSS fallback", source_name, url, response.status_code)
    return await _fetch_subreddit_rss(
        subreddit=subreddit,
        source_name=source_name,
        limit=limit,
    )


async def fetch_reddit(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch trend items from Reddit hot feeds."""
    if not sources:
        return []

    headers = {"User-Agent": REDDIT_USER_AGENT}
    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        batches = await asyncio.gather(*[_fetch_single_subreddit(client, source) for source in sources])
    return [item for batch in batches for item in batch]


async def _search_single_subreddit(
    client: httpx.AsyncClient,
    source: dict[str, Any],
    query: str,
) -> list[dict[str, Any]]:
    """Search a single subreddit for *query* using Reddit's search endpoint."""
    source_dict = _as_source_dict(source, source_type="Reddit")
    if source_dict is None:
        return []

    source_name = "reddit"
    url = ""
    try:
        subreddit = str(source_dict.get("subreddit") or "").strip().strip("/")
        if not subreddit:
            return []

        limit = max(1, min(int(source_dict.get("limit") or 10), 25))
        url = f"https://www.reddit.com/r/{subreddit}/search.json"
        params = {"q": query, "restrict_sr": "on", "sort": "relevance", "t": "week", "limit": str(limit)}
        source_name = f"r/{subreddit}"
        response = await client.get(url, params=params)
        if response.status_code >= 400:
            log.warning("Reddit search failed for %s url=%s status=%s", source_name, url, response.status_code)
            return []
        payload = response.json()
    except Exception as exc:
        log.warning("Reddit search failed for %s url=%s status=unavailable: %s", source_name, url, exc)
        return []

    items: list[dict[str, Any]] = []
    children = ((payload or {}).get("data") or {}).get("children") or []
    for child in children:
        data = child.get("data") or {}
        if data.get("stickied"):
            continue

        title = str(data.get("title") or "").strip()
        if not title:
            continue

        permalink = str(data.get("permalink") or "").strip()
        full_url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
        items.append(
            {
                "source": "reddit",
                "source_name": source_name,
                "title": title,
                "snippet": _truncate(str(data.get("selftext") or "").strip()),
                "url": full_url,
            }
        )

    if not items:
        log.warning("Reddit search returned zero usable items for %s url=%s status=%s", source_name, url, response.status_code)
    return items


async def search_reddit(query: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Search configured Reddit sources for *query*."""
    if not sources or not query.strip():
        return []

    headers = {"User-Agent": REDDIT_USER_AGENT}
    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        batches = await asyncio.gather(
            *[_search_single_subreddit(client, source, query) for source in sources],
        )
    return [item for batch in batches for item in batch]


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


async def _fetch_youtube_from_invidious(
    client: httpx.AsyncClient,
    *,
    source_name: str,
    region: str,
    category: str,
    limit: int,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"region": region}
    if category:
        params["type"] = category

    try:
        response = await client.get(YOUTUBE_TRENDING_API_URL, params=params)
        if response.status_code >= 400:
            log.warning(
                "YouTube fetch failed for %s via Invidious url=%s status=%s",
                source_name,
                YOUTUBE_TRENDING_API_URL,
                response.status_code,
            )
            return []
        payload = response.json()
    except Exception as exc:
        log.warning(
            "YouTube fetch failed for %s via Invidious url=%s status=unavailable: %s",
            source_name,
            YOUTUBE_TRENDING_API_URL,
            exc,
        )
        return []

    items: list[dict[str, Any]] = []
    for entry in list(payload or [])[:limit]:
        if not isinstance(entry, dict):
            continue

        title = str(entry.get("title") or "").strip()
        if not title:
            continue

        author = str(entry.get("author") or "").strip()
        view_count = entry.get("viewCount")
        snippet_parts = [author] if author else []
        if view_count not in (None, ""):
            snippet_parts.append(f"{view_count} views")
        video_id = str(entry.get("videoId") or "").strip()
        video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""

        items.append(
            {
                "source": "youtube",
                "source_name": source_name,
                "title": title,
                "snippet": _truncate(" | ".join(snippet_parts)),
                "url": video_url,
            }
        )

    if not items:
        log.warning(
            "YouTube fetch returned zero usable items for %s via Invidious url=%s status=%s",
            source_name,
            YOUTUBE_TRENDING_API_URL,
            response.status_code,
        )
    return items


async def _fetch_youtube_from_rss(
    *,
    source_name: str,
    region: str,
    limit: int,
) -> list[dict[str, Any]]:
    url = f"{YOUTUBE_TRENDING_RSS_URL}&gl={region}"
    if feedparser is None:
        log.warning(
            "YouTube RSS fallback failed for %s url=%s status=unavailable: feedparser is not installed",
            source_name,
            url,
        )
        return []

    try:
        parsed = await asyncio.to_thread(feedparser.parse, url)
    except Exception as exc:
        log.warning(
            "YouTube RSS fallback failed for %s url=%s status=unavailable: %s",
            source_name,
            url,
            exc,
        )
        return []

    items: list[dict[str, Any]] = []
    for entry in list(getattr(parsed, "entries", []) or [])[:limit]:
        title = str(entry.get("title") or "").strip()
        if not title:
            continue

        author = str(entry.get("author") or "").strip()
        items.append(
            {
                "source": "youtube",
                "source_name": source_name,
                "title": title,
                "snippet": _truncate(author),
                "url": str(entry.get("link") or "").strip(),
            }
        )

    if not items:
        log.warning(
            "YouTube RSS fallback returned zero usable items for %s url=%s status=%s",
            source_name,
            url,
            getattr(parsed, "status", "unknown"),
        )

    return items


async def _fetch_single_youtube(
    client: httpx.AsyncClient,
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    source_dict = _as_source_dict(source, source_type="YouTube")
    if source_dict is None:
        return []

    region = str(source_dict.get("region") or "US").strip().upper() or "US"
    category = str(source_dict.get("category") or "").strip()
    limit = _bounded_int(source_dict.get("limit"), default=10, minimum=1, maximum=10)
    source_name = str(source_dict.get("name") or f"YouTube {region}").strip() or "YouTube"

    items = await _fetch_youtube_from_invidious(
        client,
        source_name=source_name,
        region=region,
        category=category,
        limit=limit,
    )
    if items:
        return items

    return await _fetch_youtube_from_rss(
        source_name=source_name,
        region=region,
        limit=limit,
    )


async def fetch_youtube(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch trend items from YouTube trending feeds."""
    if not sources:
        return []

    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        batches = await asyncio.gather(*[_fetch_single_youtube(client, source) for source in sources])
    return [item for batch in batches for item in batch]


def _message_text(message: Any) -> str:
    text = str(
        getattr(message, "message", None)
        or getattr(message, "raw_text", None)
        or getattr(message, "text", None)
        or ""
    ).strip()
    if text:
        return text
    if getattr(message, "media", None) is not None:
        return "<media>"
    return ""


def _message_datetime_utc(message: Any) -> datetime | None:
    value = getattr(message, "date", None)
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _fetch_single_telegram_channel(
    source: dict[str, Any],
    telethon_client: Any,
) -> list[dict[str, Any]]:
    source_dict = _as_source_dict(source, source_type="Telegram")
    if source_dict is None:
        return []

    channel = source_dict.get("channel")
    if channel in (None, ""):
        return []

    source_name = str(source_dict.get("name") or channel).strip() or "telegram"
    if telethon_client is None:
        log.warning("Telegram trend fetch skipped for %s: Telethon client unavailable", source_name)
        return []

    limit = _bounded_int(source_dict.get("limit"), default=10, minimum=1, maximum=10)
    hours = _bounded_int(source_dict.get("hours"), default=6, minimum=1, maximum=72)
    manager = telethon_client
    client = getattr(telethon_client, "client", telethon_client)

    try:
        ensure_connected = getattr(manager, "ensure_connected", None)
        if callable(ensure_connected):
            await ensure_connected()
        peer = await client.get_entity(channel)
        messages = list(await client.get_messages(peer, limit=limit) or [])
    except Exception as exc:
        log.warning("Telegram trend fetch failed for %s: %s", source_name, exc)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: list[dict[str, Any]] = []
    for message in messages[:limit]:
        sent_at = _message_datetime_utc(message)
        if sent_at is not None and sent_at < cutoff:
            continue

        text = _message_text(message)
        if not text:
            continue

        items.append(
            {
                "source": "telegram",
                "source_name": source_name,
                "title": _truncate(text, 200),
                "snippet": "",
                "url": "",
            }
        )

    return items


async def fetch_telegram_channels(
    sources: list[dict[str, Any]],
    telethon_client: Any,
) -> list[dict[str, Any]]:
    """Fetch recent trend items from Telegram channels via Telethon."""
    if not sources:
        return []

    batches = await asyncio.gather(
        *[_fetch_single_telegram_channel(source, telethon_client) for source in sources]
    )
    return [item for batch in batches for item in batch]


# ---------------------------------------------------------------------------
# Hacker News (Algolia API) — tech, programming, AI
# ---------------------------------------------------------------------------

async def _fetch_single_hn(source: dict[str, Any]) -> list[dict[str, Any]]:
    source_dict = _as_source_dict(source, source_type="HN")
    if source_dict is None:
        return []

    tags = str(source_dict.get("tags") or "front_page").strip()
    limit = _bounded_int(source_dict.get("limit"), default=10, minimum=1, maximum=25)
    source_name = str(source_dict.get("name") or "Hacker News").strip()

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(HN_ALGOLIA_URL, params={"tags": tags, "hitsPerPage": limit})
            if r.status_code >= 400:
                log.warning("HN fetch failed url=%s status=%s", HN_ALGOLIA_URL, r.status_code)
                return []
            data = r.json()
    except Exception as exc:
        log.warning("HN fetch failed url=%s status=unavailable: %s", HN_ALGOLIA_URL, exc)
        return []

    items: list[dict[str, Any]] = []
    for hit in data.get("hits", []):
        title = str(hit.get("title") or "").strip()
        if not title:
            continue
        url = str(hit.get("url") or "").strip()
        if not url:
            object_id = hit.get("objectID", "")
            url = f"https://news.ycombinator.com/item?id={object_id}"
        items.append({
            "source": "hn",
            "source_name": source_name,
            "title": title,
            "snippet": _truncate(str(hit.get("story_text") or "").strip()),
            "url": url,
        })

    if not items:
        log.warning("HN fetch returned zero usable items url=%s status=%s", HN_ALGOLIA_URL, r.status_code)
    return items


async def fetch_hn(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch trend items from Hacker News via Algolia API."""
    if not sources:
        return []
    batches = await asyncio.gather(*[_fetch_single_hn(source) for source in sources])
    return [item for batch in batches for item in batch]


async def search_hn(query: str) -> list[dict[str, Any]]:
    """Search Hacker News for *query* via Algolia."""
    if not query.strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(HN_ALGOLIA_URL, params={
                "query": query,
                "tags": "story",
                "hitsPerPage": 10,
            })
            if r.status_code >= 400:
                log.warning("HN search failed url=%s status=%s", HN_ALGOLIA_URL, r.status_code)
                return []
            data = r.json()
    except Exception as exc:
        log.warning("HN search failed url=%s status=unavailable: %s", HN_ALGOLIA_URL, exc)
        return []

    items: list[dict[str, Any]] = []
    for hit in data.get("hits", []):
        title = str(hit.get("title") or "").strip()
        if not title:
            continue
        url = str(hit.get("url") or "").strip()
        if not url:
            url = f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
        items.append({
            "source": "hn",
            "source_name": "Hacker News",
            "title": title,
            "snippet": _truncate(str(hit.get("story_text") or "").strip()),
            "url": url,
        })
    if not items:
        log.warning("HN search returned zero usable items url=%s status=%s", HN_ALGOLIA_URL, r.status_code)
    return items


# ---------------------------------------------------------------------------
# DEV.to — game dev, coding tutorials, AI
# ---------------------------------------------------------------------------

async def _fetch_single_devto(source: dict[str, Any]) -> list[dict[str, Any]]:
    source_dict = _as_source_dict(source, source_type="DEV.to")
    if source_dict is None:
        return []

    tag = str(source_dict.get("tag") or "").strip()
    limit = _bounded_int(source_dict.get("limit"), default=10, minimum=1, maximum=25)
    source_name = str(source_dict.get("name") or f"DEV.to{(':' + tag) if tag else ''}").strip()

    params: dict[str, Any] = {"per_page": limit}
    if tag:
        params["tag"] = tag

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(DEVTO_API_URL, params=params)
            if r.status_code >= 400:
                log.warning("DEV.to fetch failed for %s url=%s status=%s", source_name, DEVTO_API_URL, r.status_code)
                return []
            articles = r.json()
    except Exception as exc:
        log.warning("DEV.to fetch failed for %s url=%s status=unavailable: %s", source_name, DEVTO_API_URL, exc)
        return []

    items: list[dict[str, Any]] = []
    for art in articles:
        if not isinstance(art, dict):
            continue
        title = str(art.get("title") or "").strip()
        if not title:
            continue
        items.append({
            "source": "devto",
            "source_name": source_name,
            "title": title,
            "snippet": _truncate(str(art.get("description") or "").strip()),
            "url": str(art.get("url") or "").strip(),
        })

    if not items:
        log.warning("DEV.to fetch returned zero usable items for %s url=%s status=%s", source_name, DEVTO_API_URL, r.status_code)
    return items


async def fetch_devto(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch trend items from DEV.to."""
    if not sources:
        return []
    batches = await asyncio.gather(*[_fetch_single_devto(source) for source in sources])
    return [item for batch in batches for item in batch]


async def search_devto(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search DEV.to articles for *query*."""
    if not query.strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(DEVTO_API_URL, params={"per_page": limit, "q": query})
            if r.status_code >= 400:
                log.warning("DEV.to search failed url=%s status=%s", DEVTO_API_URL, r.status_code)
                return []
            articles = r.json()
    except Exception as exc:
        log.warning("DEV.to search failed url=%s status=unavailable: %s", DEVTO_API_URL, exc)
        return []

    items: list[dict[str, Any]] = []
    for art in articles:
        if not isinstance(art, dict):
            continue
        title = str(art.get("title") or "").strip()
        if not title:
            continue
        items.append({
            "source": "devto",
            "source_name": "DEV.to",
            "title": title,
            "snippet": _truncate(str(art.get("description") or "").strip()),
            "url": str(art.get("url") or "").strip(),
        })
    if not items:
        log.warning("DEV.to search returned zero usable items url=%s status=%s", DEVTO_API_URL, r.status_code)
    return items


# ---------------------------------------------------------------------------
# Lobsters — programming, systems, tech
# ---------------------------------------------------------------------------

async def _fetch_single_lobsters(source: dict[str, Any]) -> list[dict[str, Any]]:
    source_dict = _as_source_dict(source, source_type="Lobsters")
    if source_dict is None:
        return []

    limit = _bounded_int(source_dict.get("limit"), default=10, minimum=1, maximum=25)
    source_name = str(source_dict.get("name") or "Lobsters").strip()

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(LOBSTERS_API_URL)
            if r.status_code >= 400:
                log.warning("Lobsters fetch failed url=%s status=%s", LOBSTERS_API_URL, r.status_code)
                return []
            stories = r.json()
    except Exception as exc:
        log.warning("Lobsters fetch failed url=%s status=unavailable: %s", LOBSTERS_API_URL, exc)
        return []

    items: list[dict[str, Any]] = []
    for story in stories:
        if not isinstance(story, dict):
            continue
        title = str(story.get("title") or "").strip()
        if not title:
            continue
        url = str(story.get("url") or "").strip()
        short_id = str(story.get("short_id") or "").strip()
        if not url and short_id:
            url = f"https://lobste.rs/s/{short_id}"
        items.append({
            "source": "lobsters",
            "source_name": source_name,
            "title": title,
            "snippet": _truncate(str(story.get("description") or "").strip()),
            "url": url,
        })

    items = items[:limit]
    if not items:
        log.warning("Lobsters fetch returned zero usable items url=%s status=%s", LOBSTERS_API_URL, r.status_code)
    return items


async def fetch_lobsters(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch trend items from Lobsters."""
    if not sources:
        return []
    batches = await asyncio.gather(*[_fetch_single_lobsters(source) for source in sources])
    return [item for batch in batches for item in batch]


async def fetch_all_trends() -> list[dict[str, Any]]:
    """Fetch a useful public-source trend sample for manual smoke tests."""
    fetches = {
        "hn": fetch_hn([{"limit": 10}]),
        "devto": fetch_devto([{"limit": 10}]),
        "lobsters": fetch_lobsters([{"limit": 10}]),
        "reddit": fetch_reddit([{"subreddit": "technology", "limit": 10}]),
        "youtube": fetch_youtube([{"region": "US", "limit": 10}]),
    }
    batches = await asyncio.gather(*fetches.values(), return_exceptions=True)

    items: list[dict[str, Any]] = []
    for source_name, batch in zip(fetches, batches):
        if isinstance(batch, Exception):
            log.warning("trend fetch failed for %s: %s", source_name, batch)
            continue
        items.extend(batch)
    return items
