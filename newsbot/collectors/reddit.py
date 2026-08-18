"""Reddit collector via RSS feeds.

Fetches configured subreddits in batches through Reddit's multi-subreddit
RSS endpoint (https://www.reddit.com/r/a+b+c/hot.rss), which returns entries
for every listed subreddit in ONE request. Reddit throttles this host's IP on
near-simultaneous per-subreddit request bursts — first request 200, then 429
even at 30s spacing — so batching collapses N requests into N/batch_size and
sequential group fetches with a small inter-group delay keep the host out of
the throttle window.

Per-entry subreddit attribution comes from the permalink
(https://www.reddit.com/r/<sub>/comments/...), so a batched response is split
back into per-subreddit candidates with exact source names.

Captures score and num_comments from the RSS entry metadata — engagement
signals for hype scoring.

Config (under news.sources.reddit):
  subreddits: list[str]  — e.g. ['LocalLLaMA', 'MachineLearning']
  limit: int             — per-subreddit cap (default 10)

Env:
  NEWS_REDDIT_BATCH_SIZE — subreddits per batched request (default 4)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import httpx

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None

from newsbot.collectors.base import Candidate, new_candidate, strip_html, truncate, to_iso_utc

from newsbot.collectors._shared import get_shared_semaphore

log = logging.getLogger(__name__)

REDDIT_USER_AGENT = "Mozilla/5.0 (compatible; newsbot/0.1; +https://github.com/elevenoutoften/news-bot)"

# HTTP timeout for Reddit RSS fetches.
_REDDIT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Regex to extract score and comment count from Reddit RSS entry titles
# e.g. "Some title : r/LocalLLaMA — 1.2k votes, 89 comments"
_SCORE_RE = re.compile(r"(\d+\.?\d*[km]?)\s*votes?", re.IGNORECASE)
_COMMENT_RE = re.compile(r"(\d+\.?\d*[km]?)\s*comments?", re.IGNORECASE)

# Permalink attribution: the subreddit segment of a Reddit post permalink.
_SUB_FROM_LINK_RE = re.compile(r"reddit\.com/r/([^/]+)/")

# Subreddits per batched multi-subreddit request.
_DEFAULT_BATCH_SIZE = 4

# Pacing between sequential group fetches (keeps the host out of Reddit's
# burst throttle). Constant by design — the only knob is the batch size.
_GROUP_DELAY_SECONDS = 2.0

# Reddit RSS honors ?limit=N up to 100 entries per response.
_REDDIT_RSS_LIMIT_MAX = 100

# A 429 retry never sleeps longer than this, however large Retry-After is.
_MAX_RETRY_AFTER_SECONDS = 30.0


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


def _batch_size() -> int:
    """Subreddits per batched request (NEWS_REDDIT_BATCH_SIZE, default 4)."""
    try:
        return max(1, int(os.environ.get("NEWS_REDDIT_BATCH_SIZE", str(_DEFAULT_BATCH_SIZE))))
    except ValueError:
        return _DEFAULT_BATCH_SIZE


def _retry_after_seconds(header: Any) -> float:
    """Parse a Retry-After header into seconds, clamped to the retry cap.

    Numeric headers are the common case; an HTTP-date or garbage value
    yields 0 (retry without extra sleep).
    """
    if header is None:
        return 0.0
    try:
        return max(0.0, min(float(str(header)), _MAX_RETRY_AFTER_SECONDS))
    except (TypeError, ValueError):
        return 0.0


async def _sleep(seconds: float) -> None:
    """Indirection over asyncio.sleep so tests can skip/observe pacing."""
    await asyncio.sleep(seconds)


async def _fetch_group(
    subs: list[str],
    limit: int,
    configured_by_lower: dict[str, str],
) -> list[Candidate]:
    """Fetch one batch of subreddits via the multi-subreddit RSS endpoint.

    Entries are attributed to their subreddit via the permalink; entries for
    subreddits outside the configured list (Reddit sometimes leaks neighbours
    into a group feed) are dropped. The per-subreddit cap is applied after
    attribution. Never raises: any failure logs a warning and returns [].
    """
    group_label = "+".join(subs)
    url = (
        f"https://www.reddit.com/r/{group_label}/hot.rss"
        f"?limit={min(len(subs) * limit, _REDDIT_RSS_LIMIT_MAX)}"
    )

    content = b""
    status = 0
    sem = get_shared_semaphore()
    for attempt in range(2):  # initial try + one retry after a 429
        async with sem:
            try:
                async with httpx.AsyncClient(
                    timeout=_REDDIT_TIMEOUT,
                    follow_redirects=True,
                    headers={"User-Agent": REDDIT_USER_AGENT},
                ) as client:
                    response = await client.get(url)
                    content = response.content
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After") if status == 429 else None
            except httpx.TimeoutException:
                log.warning("Reddit batch fetch timed out for %s url=%s", group_label, url)
                return []
            except Exception as exc:
                log.warning("Reddit batch fetch failed for %s url=%s: %s", group_label, url, exc)
                return []

        if status == 429 and attempt == 0:
            delay = _retry_after_seconds(retry_after)
            log.warning(
                "Reddit batch %s throttled (429); retrying once in %.1fs",
                group_label, delay,
            )
            await _sleep(delay)
            continue
        break

    if status >= 400:
        log.warning("Reddit batch fetch failed for %s url=%s status=%s", group_label, url, status)
        return []

    # Parse the already-downloaded bytes locally (no network I/O).
    try:
        parsed = feedparser.parse(content)
    except Exception as exc:
        log.warning("Reddit parse failed for %s url=%s: %s", group_label, url, exc)
        return []

    items: list[Candidate] = []
    per_sub_counts: dict[str, int] = {}
    for entry in list(getattr(parsed, "entries", []) or []):
        link = str(entry.get("link") or "").strip()
        m = _SUB_FROM_LINK_RE.search(link)
        if not m:
            continue  # unattributable entry — cannot source it responsibly
        configured_sub = configured_by_lower.get(m.group(1).lower())
        if configured_sub is None:
            continue  # not in the configured list
        if per_sub_counts.get(configured_sub, 0) >= limit:
            continue  # per-subreddit cap, applied after attribution

        title = str(entry.get("title") or "").strip()
        if not title:
            continue

        summary = entry.get("summary") or ""
        snippet = truncate(strip_html(str(summary)))
        upvotes, comments = _extract_engagement(entry)

        items.append(
            new_candidate(
                title=title,
                url=link,
                source="reddit",
                source_name=f"r/{configured_sub}",
                snippet=snippet,
                published_at=to_iso_utc(entry.get("published") or entry.get("updated")),
                upvotes=upvotes,
                comments=comments,
                raw_text=str(summary).strip() or None,
                raw_json=dict(entry),
            )
        )
        per_sub_counts[configured_sub] = per_sub_counts.get(configured_sub, 0) + 1

    if not items:
        log.warning(
            "Reddit batch fetch returned zero usable items for %s url=%s status=%s",
            group_label, url, status or "unknown",
        )
    return items


async def collect(config: dict[str, Any]) -> list[Candidate]:
    """Fetch Reddit candidates via batched multi-subreddit RSS.

    *config* is the news.sources.reddit block. Subreddits are fetched in
    groups (one request per group) SEQUENTIALLY with a small delay between
    groups — Reddit throttles this host's IP on request bursts.
    """
    subreddits: list[str] = []
    configured_by_lower: dict[str, str] = {}
    for raw in config.get("subreddits") or []:
        sub = str(raw).strip().strip("/")
        if sub and sub.lower() not in configured_by_lower:
            configured_by_lower[sub.lower()] = sub
            subreddits.append(sub)
    if not subreddits:
        return []

    if feedparser is None:
        log.warning("Reddit fetch skipped: feedparser not installed")
        return []

    limit = max(1, min(int(config.get("limit") or 10), 25))
    batch_size = _batch_size()
    groups = [subreddits[i:i + batch_size] for i in range(0, len(subreddits), batch_size)]

    results: list[Candidate] = []
    for index, group in enumerate(groups):
        if index:
            await _sleep(_GROUP_DELAY_SECONDS)
        try:
            results.extend(await _fetch_group(group, limit, configured_by_lower))
        except Exception as exc:  # defensive: the collector never raises
            log.warning("Reddit batch fetch error for %s: %s", "+".join(group), exc)
    return results
