"""Hacker News collector via the Algolia API.

Algolia returns points and num_comments per hit, which we capture
for hype scoring.

Config (under news.sources.hackernews):
  queries: list[str]   — optional search queries (defaults to front_page)
  tags: str             — Algolia tag filter, default 'front_page'
  limit: int            — per-query hit cap, 1-25 (default 10)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from newsbot.collectors.base import new_candidate, strip_html, truncate, to_iso_utc
from newsbot.collectors._shared import get_shared_semaphore

log = logging.getLogger(__name__)

HN_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"


async def _fetch_one(client: httpx.AsyncClient, *, params: dict[str, Any], source_name: str) -> list[dict[str, Any]]:
    sem = get_shared_semaphore()
    async with sem:
        try:
            r = await client.get(HN_ALGOLIA_URL, params=params)
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

        items.append(
            new_candidate(
                title=title,
                url=url,
                source="hn",
                source_name=source_name,
                snippet=truncate(strip_html(str(hit.get("story_text") or ""))),
                published_at=to_iso_utc(hit.get("created_at")),
                upvotes=int(hit.get("points") or 0) or None,
                comments=int(hit.get("num_comments") or 0) or None,
                raw_text=str(hit.get("story_text") or "").strip() or None,
                raw_json=hit,
            )
        )

    if not items:
        log.warning("HN fetch returned zero usable items url=%s params=%s", HN_ALGOLIA_URL, params)
    return items


async def collect(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch HN candidates. *config* is the news.sources.hackernews block."""
    if not config:
        return []

    queries = config.get("queries") or []
    tags = str(config.get("tags") or "front_page").strip()
    limit = max(1, min(int(config.get("limit") or 10), 25))
    source_name = str(config.get("name") or "Hacker News").strip()

    requests: list[dict[str, Any]] = []
    if queries:
        for q in queries:
            requests.append({"query": str(q), "tags": tags, "hitsPerPage": limit})
    else:
        requests.append({"tags": tags, "hitsPerPage": limit})

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        results = []
        for params in requests:
            results.extend(await _fetch_one(client, params=params, source_name=source_name))
    return results