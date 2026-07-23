"""Reddit collector via the public JSON API.

Fetches /hot.json per configured subreddit. Captures score, num_comments,
and upvote_ratio — engagement signals for hype scoring.

Config (under news.sources.reddit):
  subreddits: list[str]  — e.g. ['LocalLLaMA', 'MachineLearning']
  limit: int             — per-subreddit cap, 1-25 (default 10)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from newsbot.collectors.base import new_candidate, truncate, to_iso_utc

log = logging.getLogger(__name__)

REDDIT_USER_AGENT = "Mozilla/5.0 (compatible; newsbot/0.1; +https://github.com/elevenoutoften/news-bot)"


async def _fetch_one(client: httpx.AsyncClient, *, subreddit: str, limit: int) -> list[dict[str, Any]]:
    source_name = f"r/{subreddit}"
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}"
    try:
        r = await client.get(url)
        if r.status_code >= 400:
            log.warning("Reddit fetch failed for %s url=%s status=%s", source_name, url, r.status_code)
            return []
        payload = r.json()
    except Exception as exc:
        log.warning("Reddit fetch failed for %s url=%s status=unavailable: %s", source_name, url, exc)
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
            new_candidate(
                title=title,
                url=full_url,
                source="reddit",
                source_name=source_name,
                snippet=truncate(str(data.get("selftext") or "")),
                published_at=to_iso_utc(data.get("created_utc")),
                upvotes=int(data.get("score") or 0) or None,
                comments=int(data.get("num_comments") or 0) or None,
                upvote_ratio=float(data.get("upvote_ratio") or 0.0) or None,
                raw_text=str(data.get("selftext") or "").strip() or None,
                raw_json=data,
            )
        )

    if not items:
        log.warning("Reddit fetch returned zero usable items for %s url=%s status=%s", source_name, url, r.status_code)
    return items


async def collect(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch Reddit candidates. *config* is the news.sources.reddit block."""
    subreddits = config.get("subreddits") or []
    if not subreddits:
        return []

    limit = max(1, min(int(config.get("limit") or 10), 25))
    headers = {"User-Agent": REDDIT_USER_AGENT}
    timeout = httpx.Timeout(15.0)

    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        results = []
        for sub in subreddits:
            sub = str(sub).strip().strip("/")
            if sub:
                results.extend(await _fetch_one(client, subreddit=sub, limit=limit))
    return results