"""Hugging Face Papers collector.

Fetches the daily trending papers from huggingface.co/papers. The page
exposes a JSON sidecar at /api/daily_papers (community-known endpoint)
that includes upvotes and linked models/datasets/spaces — exactly the
engagement signals the news bot scores on.

Config (under news.sources.huggingface_papers):
  limit: int  — cap, 1-30 (default 10)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from newsbot.collectors.base import Candidate, new_candidate, truncate
from newsbot.collectors._shared import get_shared_semaphore
from newsbot.httpclient import owned_client

log = logging.getLogger(__name__)

HF_DAILY_PAPERS_URL = "https://huggingface.co/api/daily_papers"
_HF_TIMEOUT = httpx.Timeout(20.0)


async def collect(config: dict[str, Any], client: httpx.AsyncClient | None = None) -> list[Candidate]:
    """Fetch HF Papers candidates. *config* is the news.sources.huggingface_papers block."""
    if not config:
        # enabled but empty config — still fetch defaults
        pass

    limit = max(1, min(int(config.get("limit") or 10), 30))
    source_name = "Hugging Face Papers"

    sem = get_shared_semaphore()
    async with sem:
        try:
            async with owned_client(client, client_cls=httpx.AsyncClient, timeout=_HF_TIMEOUT, follow_redirects=True) as c:
                r = await c.get(HF_DAILY_PAPERS_URL, timeout=_HF_TIMEOUT)
                if r.status_code >= 400:
                    log.warning("HF Papers fetch failed url=%s status=%s", HF_DAILY_PAPERS_URL, r.status_code)
                    return []
                data = r.json()
        except Exception as exc:
            log.warning("HF Papers fetch failed url=%s status=unavailable: %s", HF_DAILY_PAPERS_URL, exc)
            return []

    # The endpoint returns a list of paper objects.
    papers = data if isinstance(data, list) else []

    items: list[Candidate] = []
    for paper in papers[:limit]:
        if not isinstance(paper, dict):
            continue

        paper_inner = paper.get("paper") or paper  # daily_papers nests under 'paper'
        title = str(paper_inner.get("title") or paper.get("title") or "").strip()
        if not title:
            continue

        paper_id = str(paper_inner.get("id") or paper.get("paperId") or "").strip()
        url = f"https://huggingface.co/papers/{paper_id}" if paper_id else ""

        upvotes = paper.get("paper", {}).get("upvotes") if isinstance(paper.get("paper"), dict) else paper.get("upvotes")
        comments = paper.get("paper", {}).get("commentsCount") if isinstance(paper.get("paper"), dict) else paper.get("commentsCount")

        items.append(
            new_candidate(
                title=title,
                url=url,
                source="huggingface_papers",
                source_name=source_name,
                snippet=truncate(str(paper_inner.get("summary") or "").strip()),
                published_at=paper_inner.get("publishedAt") or paper.get("publishedAt"),
                upvotes=int(upvotes or 0) or None,
                comments=int(comments or 0) if comments is not None else None,
                raw_text=str(paper_inner.get("summary") or "").strip() or None,
                raw_json=paper,
            )
        )

    if not items:
        log.warning("HF Papers returned zero usable items url=%s", HF_DAILY_PAPERS_URL)
    return items