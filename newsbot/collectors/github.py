"""GitHub trending/search collector.

Uses the public /search/repositories endpoint. Captures stargazers_count,
forks_count, created_at, updated_at, topics — engagement signals the
news bot scores on.

Safety filters (per architecture spec §7.3):
  - penalize repos with no description (proxy for no README)
  - penalize suspiciously young repos with huge stars but low forks/issues
  - penalize crypto / scam / piracy keywords

Config (under news.sources.github):
  queries: list[str]  — e.g. ['llm', 'agent', 'coding-agent', 'unity']
  limit: int           — per-query cap, 1-30 (default 30)
  sort: str            — 'stars' | 'updated' | 'forks' (default 'stars')
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from newsbot.collectors.base import new_candidate, truncate, to_iso_utc

log = logging.getLogger(__name__)

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"

# Penalty keywords — repos whose name/description matches these get down-weighted
# by setting a low score multiplier (applied via the 'penalty' field, read by scoring).
PENALTY_KEYWORDS = (
    "crypto", "scam", "piracy", "nft", "token", "airdrop", "casino",
    "gamble", "crack", "leak", "cheat", "hack-tool",
)

# A repo younger than this many days with stars but near-zero forks is suspicious.
SUSPICIOUS_AGE_DAYS = 14
SUSPICIOUS_MIN_STARS = 500
SUSPICIOUS_MAX_FORKS = 5


def _has_penalty_keyword(*text_bits: str) -> bool:
    blob = " ".join(t for t in text_bits if t).lower()
    return any(kw in blob for kw in PENALTY_KEYWORDS)


def _is_suspicious(repo: dict[str, Any]) -> bool:
    created = to_iso_utc(repo.get("created_at"))
    stars = int(repo.get("stargazers_count") or 0)
    forks = int(repo.get("forks_count") or 0)
    if not created or stars < SUSPICIOUS_MIN_STARS or forks > SUSPICIOUS_MAX_FORKS:
        return False
    # Suspicious: very young, high stars, almost no forks.
    from datetime import datetime, timezone
    try:
        created_dt = datetime.fromisoformat(created)
    except ValueError:
        return False
    age_days = (datetime.now(timezone.utc) - created_dt).days
    return age_days <= SUSPICIOUS_AGE_DAYS


async def _fetch_one(client: httpx.AsyncClient, *, query: str, limit: int, sort: str) -> list[dict[str, Any]]:
    params = {"q": query, "sort": sort, "order": "desc", "per_page": limit}
    try:
        r = await client.get(GITHUB_SEARCH_URL, params=params)
        if r.status_code >= 400:
            log.warning("GitHub search failed query=%r status=%s", query, r.status_code)
            return []
        data = r.json()
    except Exception as exc:
        log.warning("GitHub search failed query=%r status=unavailable: %s", query, exc)
        return []

    items: list[dict[str, Any]] = []
    for repo in data.get("items", []):
        full_name = str(repo.get("full_name") or "").strip()
        if not full_name:
            continue

        html_url = str(repo.get("html_url") or "").strip()
        description = str(repo.get("description") or "").strip()
        topics = repo.get("topics") or []

        # Safety filters: stamp a penalty multiplier on the candidate; scoring applies it.
        penalty = 1.0
        if not description:
            penalty *= 0.5  # no description => likely low-quality
        if _has_penalty_keyword(full_name, description, " ".join(topics)):
            penalty *= 0.1
        if _is_suspicious(repo):
            penalty *= 0.2

        # Use created_at as the publication date — when the repo was first created.
        # Using pushed_at would make old repos with recent maintenance pushes
        # appear as freshly published articles.
        published = to_iso_utc(repo.get("created_at"))
        pushed = to_iso_utc(repo.get("pushed_at"))

        items.append(
            new_candidate(
                title=full_name,
                url=html_url,
                source="github",
                source_name="GitHub Trending",
                snippet=truncate(description),
                published_at=published,
                stars=int(repo.get("stargazers_count") or 0) or None,
                forks=int(repo.get("forks_count") or 0) or None,
                category=None,
                raw_text=description or None,
                raw_json={**repo, "_last_activity": pushed},
            )
        )
        # Stash the penalty multiplier on the candidate so scoring can apply it.
        items[-1]["penalty"] = penalty

    if not items:
        log.warning("GitHub search returned zero usable items query=%r", query)
    return items


async def collect(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch GitHub candidates. *config* is the news.sources.github block."""
    queries = config.get("queries") or []
    if not queries:
        return []

    limit = max(1, min(int(config.get("limit") or 30), 30))
    sort = str(config.get("sort") or "stars").strip() or "stars"
    headers = {
        "User-Agent": "newsbot/0.1",
        "Accept": "application/vnd.github+json",
    }
    # Optional: use GITHUB_TOKEN to get higher rate limits (60/hr → 5000/hr)
    github_token = os.getenv("GITHUB_TOKEN", "").strip()
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    timeout = httpx.Timeout(20.0)

    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        # Fetch queries concurrently for bounded latency.
        tasks = [_fetch_one(client, query=str(q).strip(), limit=limit, sort=sort)
                 for q in queries if str(q).strip()]
        batches = await asyncio.gather(*tasks, return_exceptions=True)
        results = []
        for batch in batches:
            if isinstance(batch, Exception):
                log.warning("GitHub query failed: %s", batch)
                continue
            results.extend(batch)
    return results