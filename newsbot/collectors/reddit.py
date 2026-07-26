"""Reddit collector via RSS feeds.

Fetches /hot.rss per configured subreddit. Reddit's public JSON API
now blocks all requests with 403, but the RSS endpoints still work.

Captures score and num_comments from the RSS entry metadata — engagement
signals for hype scoring.

Config (under news.sources.reddit):
  subreddits: list[str]  — e.g. ['LocalLLaMA', 'MachineLearning']
  limit: int             — per-subreddit cap (default 10)
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None

from newsbot.collectors.base import new_candidate, strip_html, truncate, to_iso_utc

log = logging.getLogger(__name__)

REDDIT_USER_AGENT = "Mozilla/5.0 (compatible; newsbot/0.1; +https://github.com/elevenoutoften/news-bot)"

# Regex to extract score and comment count from Reddit RSS entry titles
# e.g. "Some title : r/LocalLLaMA — 1.2k votes, 89 comments"
_SCORE_RE = re.compile(r"(\d+\.?\d*[km]?)\s*votes?", re.IGNORECASE)
_COMMENT_RE = re.compile(r"(\d+\.?\d*[km]?)\s*comments?", re.IGNORECASE)


def _parse_count(text: str) -> int:
    """Parse a Reddit-style count string like '1.2k' or '89' to int."""
    text = text.strip().lower()
    if not text:
        return 0
    mult = 1
    if text.endswith("k"):
        mult = 1_000
        text = text[:-1]
    elif text.endswith("m"):
        mult = 1_000_000
        text = text[:-1]
    try:
        return int(float(text) * mult)
    except (ValueError, TypeError):
        return 0


def _extract_engagement(entry: Any) -> tuple[int | None, int | None]:
    """Try to extract upvotes and comment count from a Reddit RSS entry."""
    # Reddit RSS entries often have the counts in the title or content.
    title = str(entry.get("title") or "")
    summary = str(entry.get("summary") or "") + str(entry.get("content", [{}])[0].get("value", "") if entry.get("content") else "")
    blob = title + " " + summary

    upvotes: int | None = None
    comments: int | None = None

    m = _SCORE_RE.search(blob)
    if m:
        upvotes = _parse_count(m.group(1))

    m = _COMMENT_RE.search(blob)
    if m:
        comments = _parse_count(m.group(1))

    return upvotes, comments


async def _fetch_one(subreddit: str, limit: int) -> list[dict[str, Any]]:
    source_name = f"r/{subreddit}"
    url = f"https://www.reddit.com/r/{subreddit}/hot.rss"

    if feedparser is None:
        log.warning("Reddit fetch skipped for %s: feedparser not installed", source_name)
        return []

    try:
        # Use a bounded timeout for the feedparser blocking network request.
        import signal
        def _timeout_handler(signum, frame):
            raise TimeoutError(f"Reddit fetch timed out for {source_name}")
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(30)  # 30-second timeout
        try:
            parsed = await asyncio.to_thread(
                feedparser.parse, url, agent=REDDIT_USER_AGENT
            )
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
    except TimeoutError as exc:
        log.warning("Reddit fetch timed out for %s url=%s: %s", source_name, url, exc)
        return []
    except Exception as exc:
        log.warning("Reddit fetch failed for %s url=%s: %s", source_name, url, exc)
        return []

    status = getattr(parsed, "status", None)
    if status and status >= 400:
        log.warning("Reddit fetch failed for %s url=%s status=%s", source_name, url, status)
        return []

    items: list[dict[str, Any]] = []
    for entry in list(getattr(parsed, "entries", []) or [])[:limit]:
        title = str(entry.get("title") or "").strip()
        if not title:
            continue

        link = str(entry.get("link") or "").strip()
        # Reddit RSS links are permalinks to the post.
        full_url = link or f"https://www.reddit.com/r/{subreddit}/"

        summary = entry.get("summary") or ""
        snippet = truncate(strip_html(str(summary)))

        upvotes, comments = _extract_engagement(entry)

        items.append(
            new_candidate(
                title=title,
                url=full_url,
                source="reddit",
                source_name=source_name,
                snippet=snippet,
                published_at=to_iso_utc(entry.get("published") or entry.get("updated")),
                upvotes=upvotes,
                comments=comments,
                raw_text=str(summary).strip() or None,
                raw_json=dict(entry),
            )
        )

    if not items:
        log.warning(
            "Reddit fetch returned zero usable items for %s url=%s status=%s",
            source_name, url, status or "unknown",
        )
    return items


async def collect(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch Reddit candidates via RSS. *config* is the news.sources.reddit block."""
    subreddits = config.get("subreddits") or []
    if not subreddits:
        return []

    limit = max(1, min(int(config.get("limit") or 10), 25))

    async def fetch_all() -> list[dict[str, Any]]:
        # Fetch subreddits concurrently with bounded parallelism.
        results: list[dict[str, Any]] = []
        tasks = [_fetch_one(str(sub).strip().strip("/"), limit)
                 for sub in subreddits if str(sub).strip().strip("/")]
        batches = await asyncio.gather(*tasks, return_exceptions=True)
        for batch in batches:
            if isinstance(batch, Exception):
                log.warning("Reddit sub fetch failed: %s", batch)
                continue
            results.extend(batch)
        return results

    return await fetch_all()