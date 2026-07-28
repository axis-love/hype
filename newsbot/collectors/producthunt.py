"""Product Hunt collector via the official GraphQL API.

PH requires an API token (PH_API_KEY env var). If unset, the collector
returns [] and logs a skip — the pipeline continues without PH.

Captures votesCount, commentsCount, topics — engagement signals for hype
scoring. PH is weighted lower than HN/Reddit/GitHub (lots of marketing
noise) unless the same product appears on multiple sources (cross-source
bonus).

Config (under news.sources.producthunt):
  topics: list[str]  — e.g. ['artificial-intelligence', 'developer-tools']
  limit: int           — per-topic cap, 1-20 (default 10)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from newsbot.collectors.base import Candidate, new_candidate, truncate

from newsbot.collectors._shared import get_shared_semaphore

log = logging.getLogger(__name__)

PH_GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"

# Fetch posts in a topic, newest first. PH's GraphQL schema requires a
# first/after cursor; we use first=<limit> and no after for the first page.
POSTS_QUERY = """
query ($first: Int!, $topic: String!) {
  topic(slug: $topic) {
    posts(first: $first, order: NEWEST) {
      edges {
        node {
          id
          name
          tagline
          url
          website
          votesCount
          commentsCount
          createdAt
          topics(first: 5) { edges { node { name slug } } }
        }
      }
    }
  }
}
"""


async def _fetch_topic(client: httpx.AsyncClient, *, topic: str, limit: int, token: str) -> list[Candidate]:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "newsbot/0.1"}
    sem = get_shared_semaphore()
    async with sem:
        try:
            r = await client.post(
                PH_GRAPHQL_URL,
                json={"query": POSTS_QUERY, "variables": {"first": limit, "topic": topic}},
                headers=headers,
            )
            if r.status_code >= 400:
                log.warning("PH fetch failed topic=%r status=%s", topic, r.status_code)
                return []
            data = r.json()
        except Exception as exc:
            log.warning("PH fetch failed topic=%r status=unavailable: %s", topic, exc)
            return []

    topic_node = ((data or {}).get("data") or {}).get("topic") or {}
    edges = (topic_node.get("posts") or {}).get("edges") or []
    items: list[Candidate] = []
    for edge in edges:
        post = (edge or {}).get("node") or {}
        name = str(post.get("name") or "").strip()
        if not name:
            continue

        tagline = str(post.get("tagline") or "").strip()
        url = str(post.get("url") or "").strip()
        if url and not url.startswith("http"):
            url = f"https://www.producthunt.com{url}"

        post_topics = [
            str((t.get("node") or {}).get("name") or "")
            for t in (post.get("topics") or {}).get("edges") or []
        ]

        items.append(
            new_candidate(
                title=name,
                url=url,
                source="producthunt",
                source_name=f"Product Hunt · {topic}",
                snippet=truncate(tagline),
                published_at=post.get("createdAt"),
                upvotes=int(post.get("votesCount") or 0) or None,
                comments=int(post.get("commentsCount") or 0) or None,
                raw_text=tagline or None,
                raw_json={**post, "_topics": [t for t in post_topics if t]},
            )
        )

    if not items:
        log.warning("PH topic %r returned zero usable items", topic)
    return items


async def collect(config: dict[str, Any]) -> list[Candidate]:
    """Fetch PH candidates. *config* is the news.sources.producthunt block."""
    topics = config.get("topics") or []
    if not topics:
        return []

    token = os.getenv("PH_API_KEY", "").strip()
    if not token:
        log.info("PH_API_KEY not set; skipping Product Hunt collector")
        return []

    limit = max(1, min(int(config.get("limit") or 10), 20))
    timeout = httpx.Timeout(20.0)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        results = []
        for topic in topics:
            topic = str(topic).strip()
            if topic:
                results.extend(await _fetch_topic(client, topic=topic, limit=limit, token=token))
    return results