"""Canonical Candidate shape and shared normalization helpers.

A Candidate is a normalized news item. Collectors return plain dicts
matching this shape; downstream stages (scoring, dedupe, summarizer)
read these keys.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Optional


# Canonical candidate fields.
CANDIDATE_KEYS: tuple[str, ...] = (
    "title",
    "url",
    "source",            # 'hn' | 'reddit' | 'github' | 'producthunt' | 'huggingface_papers' | 'rss'
    "source_name",        # human label, e.g. 'r/LocalLLaMA', 'OpenAI blog'
    "source_type",        # alias of source; kept for doc parity
    "snippet",            # short text excerpt for prompts and dedupe
    "published_at",       # ISO 8601 UTC, or None
    "score",              # computed hype score (filled by scoring.py)
    "upvotes",            # HN points / Reddit score / PH votes / HF upvotes
    "comments",           # comment count
    "stars",              # GitHub stargazers_count
    "forks",              # GitHub forks_count
    "reposts",            # reserved (Threads/X cross-post count, unused in v0.1)
    "upvote_ratio",       # Reddit only
    "velocity",           # reserved (stars/hour, unused in v0.1)
    "category",           # LLM-assigned in Pass A, None until then
    "raw_text",           # original API hit text (e.g. story_text, selftext)
    "extracted_text",     # reserved (trafilatura-extracted body, unused in v0.1)
    "crosspost_count",    # filled by dedupe.py: how many distinct sources carried this
    "raw_json",           # original API payload (debug)
)


def new_candidate(
    *,
    title: str,
    url: str,
    source: str,
    source_name: str,
    **extra: Any,
) -> dict[str, Any]:
    """Build a Candidate dict with sane null defaults for every key."""
    c: dict[str, Any] = {k: None for k in CANDIDATE_KEYS}
    c["title"] = title
    c["url"] = url or None
    c["source"] = source
    c["source_name"] = source_name
    c["source_type"] = source
    c["score"] = 0.0
    c["crosspost_count"] = 1
    for k, v in extra.items():
        if k in c:
            c[k] = v
    return c


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    """Strip HTML tags and collapse whitespace; unescape entities."""
    no_tags = _TAG_RE.sub(" ", text or "")
    return html.unescape(_WS_RE.sub(" ", no_tags)).strip()


def truncate(text: str, limit: int = 200) -> str:
    """Collapse whitespace and truncate to *limit* chars with an ellipsis."""
    cleaned = _WS_RE.sub(" ", text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def to_iso_utc(value: Any) -> Optional[str]:
    """Best-effort conversion of common datetime-ish values to ISO 8601 UTC.

    Accepts: epoch seconds (int/float/str), ISO strings, datetime objects.
    Returns None if the value can't be parsed.
    """
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")

    # Epoch seconds (Reddit uses this).
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None

    s = str(value).strip()
    if not s:
        return None

    # Try epoch-as-string first (Reddit's created_utc is sometimes a float str).
    try:
        return datetime.fromtimestamp(float(s), tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        pass

    # Fall back to ISO parsing.
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None