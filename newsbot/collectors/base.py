"""Canonical Candidate shape and shared normalization helpers.

A Candidate is a TypedDict — a runtime dict with typed keys. Collectors
return dicts from new_candidate(). Downstream stages consume them as
plain dicts. validate_candidate() runs the construction-time checks that
used to live on the dataclass.
"""

from __future__ import annotations

import html
import math
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional, TypedDict

import httpx


# --- Source identifier validation ---

#: Canonical source keys accepted in config ``sources`` blocks.
#: This is the single source of truth — config.py imports it for validation,
#: and main.py's COLLECTORS registry keys must match this set (tested).
#: Adding a collector means adding its key here + a COLLECTORS entry.
VALID_SOURCE_KEYS: frozenset[str] = frozenset({
    "hackernews",
    "reddit",
    "github",
    "rss",
    "huggingface_papers",
    "trends",
})

#: Alias normalization: maps alternative names to canonical source IDs.
#: Used by new_candidate so collectors can emit either form (e.g. "hackernews"
#: in config vs "hn" in Candidate.source).
_SOURCE_ALIASES: dict[str, str] = {
    "hackernews": "hn",
}

#: All accepted Candidate source IDs: canonical config keys + alias keys
#: and targets. Derived from VALID_SOURCE_KEYS so the set stays in sync
#: automatically when a collector is added or removed.
_KNOWN_SOURCES: frozenset[str] = VALID_SOURCE_KEYS | frozenset(
    _SOURCE_ALIASES.keys()
) | frozenset(_SOURCE_ALIASES.values())


def _normalize_source_id(src: str) -> str:
    """Normalize a source identifier, applying aliases.

    Raises ValueError for unknown source IDs.
    """
    s = (src or "").strip().lower()
    if not s:
        raise ValueError("Candidate source must be a non-empty string")
    canonical = _SOURCE_ALIASES.get(s, s)
    if canonical not in _KNOWN_SOURCES:
        raise ValueError(
            f"Unknown Candidate source {src!r}. "
            f"Known: {', '.join(sorted(_KNOWN_SOURCES))}"
        )
    return canonical


class Candidate(TypedDict, total=False):
    """A normalized news candidate from any source. Runtime type is dict."""

    title: str
    url: str
    source: str
    source_name: str
    source_type: str
    snippet: str | None
    published_at: str | None
    score: float
    upvotes: int | None
    comments: int | None
    stars: int | None
    forks: int | None
    reposts: int | None
    upvote_ratio: float | None
    velocity: float | None
    category: str | None
    raw_text: str | None
    extracted_text: str | None
    crosspost_count: int
    raw_json: dict[str, Any] | None
    candidate_id: str | None
    importance: int | None
    reason: str | None
    short_summary: str | None
    penalty: float
    contributing_sources: list[str]
    contributing_urls: list[str]
    score_breakdown: dict[str, Any] | None


_KNOWN_CANDIDATE_FIELDS = frozenset(Candidate.__annotations__)


def validate_candidate(d: dict[str, Any]) -> None:
    """Validate required fields and engagement values. Mutates source/source_type."""
    if not d.get("title"):
        raise ValueError("Candidate requires a non-empty title")
    if not d.get("source"):
        raise ValueError("Candidate requires a non-empty source")
    d["source"] = _normalize_source_id(str(d.get("source") or ""))
    if not d.get("source_name"):
        raise ValueError("Candidate requires a non-empty source_name")
    url_str = str(d.get("url") or "").strip()
    if not url_str:
        raise ValueError("Candidate requires a non-empty url")
    if not (url_str.startswith("http://") or url_str.startswith("https://")):
        raise ValueError(
            f"Candidate.url must have http:// or https:// scheme, got {url_str!r}"
        )
    d["url"] = url_str
    if not d.get("source_type"):
        d["source_type"] = d["source"]
    for fname in ("upvotes", "comments", "stars", "forks", "reposts",
                  "crosspost_count"):
        val = d.get(fname)
        if val is not None:
            if isinstance(val, bool):
                raise ValueError(
                    f"Candidate.{fname} must be numeric, got bool: {val}"
                )
            if not isinstance(val, (int, float)):
                raise ValueError(
                    f"Candidate.{fname} must be numeric, got {type(val).__name__}"
                )
            if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                raise ValueError(f"Candidate.{fname} must be finite, got {val}")
    for fname in ("upvotes", "comments", "stars", "forks", "reposts"):
        val = d.get(fname)
        if val is not None and val < 0:
            raise ValueError(f"Candidate.{fname} must be non-negative, got {val}")
    score = d.get("score", 0.0)
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError(f"Candidate.score must be numeric, got {type(score).__name__}")
    if isinstance(score, float) and (math.isnan(score) or math.isinf(score)):
        raise ValueError(f"Candidate.score must be finite, got {score}")
    if score < 0:
        raise ValueError(f"Candidate.score must be non-negative, got {score}")
    penalty = d.get("penalty", 1.0)
    if isinstance(penalty, bool) or not isinstance(penalty, (int, float)):
        raise ValueError(f"Candidate.penalty must be numeric, got {type(penalty).__name__}")
    if isinstance(penalty, float) and (math.isnan(penalty) or math.isinf(penalty)):
        raise ValueError(f"Candidate.penalty must be finite, got {penalty}")
    if penalty < 0:
        raise ValueError(f"Candidate.penalty must be non-negative, got {penalty}")
    upvote_ratio = d.get("upvote_ratio")
    if upvote_ratio is not None:
        if isinstance(upvote_ratio, bool) or not isinstance(upvote_ratio, (int, float)):
            raise ValueError(
                f"Candidate.upvote_ratio must be numeric, got {type(upvote_ratio).__name__}"
            )
        if not (0.0 <= float(upvote_ratio) <= 1.0):
            raise ValueError(
                f"Candidate.upvote_ratio must be in [0,1], got {upvote_ratio}"
            )
    published_at = d.get("published_at")
    if published_at is not None:
        ts = str(published_at).strip()
        if ts:
            try:
                parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(
                    f"Candidate.published_at must be valid ISO 8601, got {ts!r}"
                )
            if parsed.year < 2000 or parsed.year > 2100:
                raise ValueError(
                    f"Candidate.published_at year {parsed.year} out of range [2000, 2100]"
                )


def new_candidate(
    *,
    title: str,
    url: str,
    source: str,
    source_name: str,
    **extra: Any,
) -> Candidate:
    """Build a validated Candidate dict.

    Unknown fields raise ValueError — typos are caught at construction.
    Invalid engagement values raise ValueError — no catch-and-continue.
    """
    for k in extra:
        if k not in _KNOWN_CANDIDATE_FIELDS:
            raise ValueError(
                f"new_candidate: unknown field {k!r} — possible typo. "
                f"Known: {', '.join(sorted(_KNOWN_CANDIDATE_FIELDS))}"
            )
    d: dict[str, Any] = {
        "title": title,
        "url": url,
        "source": source,
        "source_name": source_name,
        "source_type": extra.get("source_type") or "",
        "snippet": None,
        "published_at": None,
        "score": 0.0,
        "upvotes": None,
        "comments": None,
        "stars": None,
        "forks": None,
        "reposts": None,
        "upvote_ratio": None,
        "velocity": None,
        "category": None,
        "raw_text": None,
        "extracted_text": None,
        "crosspost_count": 1,
        "raw_json": None,
        "candidate_id": None,
        "importance": None,
        "reason": None,
        "short_summary": None,
        "penalty": 1.0,
        "contributing_sources": [],
        "contributing_urls": [],
        "score_breakdown": None,
    }
    d.update(extra)
    if d.get("score") is None:
        d["score"] = 0.0
    if d.get("penalty") is None:
        d["penalty"] = 1.0
    if d.get("crosspost_count") is None:
        d["crosspost_count"] = 1
    if d.get("contributing_sources") is None:
        d["contributing_sources"] = []
    if d.get("contributing_urls") is None:
        d["contributing_urls"] = []
    validate_candidate(d)
    return d  # type: ignore[return-value]


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


@asynccontextmanager
async def owned_client(
    existing: httpx.AsyncClient | None = None,
    **kwargs: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    """Use *existing* if given, otherwise open (and close) a new client."""
    if existing is not None:
        yield existing
        return
    async with httpx.AsyncClient(**kwargs) as client:
        yield client
