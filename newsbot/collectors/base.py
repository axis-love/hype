"""Canonical Candidate shape and shared normalization helpers.

A Candidate is a normalized news item. Collectors return Candidate
dataclass instances (via new_candidate or from_dict). Downstream stages
(scoring, dedupe, summarizer) consume candidates via the .to_dict()
method or direct attribute access.

The dataclass provides:
  - Typed fields with defaults (no more silent key typos)
  - Validation at construction time (empty title/source raises)
  - to_dict() / from_dict() for backward-compatible dict interop
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class Candidate:
    """A normalized news candidate from any source.

    Typed model replacing the previous dict-with-many-optional-fields.
    All collectors produce Candidate instances; downstream stages can
    use .to_dict() for backward compatibility with dict-based code.
    """
    # Required identity fields.
    title: str
    url: str
    source: str          # 'hn' | 'reddit' | 'github' | 'producthunt' | 'rss' | etc.
    source_name: str     # Human label, e.g. 'r/LocalLLaMA', 'OpenAI blog'

    # Optional fields with defaults.
    source_type: str = ""
    snippet: Optional[str] = None
    published_at: Optional[str] = None
    score: float = 0.0
    upvotes: Optional[int] = None
    comments: Optional[int] = None
    stars: Optional[int] = None
    forks: Optional[int] = None
    reposts: Optional[int] = None
    upvote_ratio: Optional[float] = None
    velocity: Optional[float] = None
    category: Optional[str] = None
    raw_text: Optional[str] = None
    extracted_text: Optional[str] = None
    crosspost_count: int = 1
    raw_json: Optional[dict[str, Any]] = None

    # LLM-assigned fields (filled by summarizer).
    candidate_id: Optional[str] = None
    importance: Optional[int] = None
    reason: Optional[str] = None
    short_summary: Optional[str] = None

    # Dedup/scoring fields (filled by dedupe.py).
    penalty: float = 1.0
    contributing_sources: list[str] = field(default_factory=list, repr=False)
    _source_names_set: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        """Validate required fields and engagement values at construction time."""
        if not self.title:
            raise ValueError("Candidate requires a non-empty title")
        if not self.source:
            raise ValueError("Candidate requires a non-empty source")
        if not self.source_name:
            raise ValueError("Candidate requires a non-empty source_name")
        if not self.url:
            raise ValueError("Candidate requires a non-empty url")
        if not self.source_type:
            self.source_type = self.source
        # Validate engagement values are non-negative (if provided).
        for fname in ("upvotes", "comments", "stars", "forks", "reposts"):
            val = getattr(self, fname)
            if val is not None and val < 0:
                raise ValueError(f"Candidate.{fname} must be non-negative, got {val}")
        if self.score < 0:
            raise ValueError(f"Candidate.score must be non-negative, got {self.score}")
        if self.penalty < 0:
            raise ValueError(f"Candidate.penalty must be non-negative, got {self.penalty}")
        if self.upvote_ratio is not None and not (0.0 <= self.upvote_ratio <= 1.0):
            raise ValueError(f"Candidate.upvote_ratio must be in [0,1], got {self.upvote_ratio}")

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dict compatible with existing pipeline code."""
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "source_name": self.source_name,
            "source_type": self.source_type,
            "snippet": self.snippet,
            "published_at": self.published_at,
            "score": self.score,
            "upvotes": self.upvotes,
            "comments": self.comments,
            "stars": self.stars,
            "forks": self.forks,
            "reposts": self.reposts,
            "upvote_ratio": self.upvote_ratio,
            "velocity": self.velocity,
            "category": self.category,
            "raw_text": self.raw_text,
            "extracted_text": self.extracted_text,
            "crosspost_count": self.crosspost_count,
            "raw_json": self.raw_json,
            "candidate_id": self.candidate_id,
            "importance": self.importance,
            "reason": self.reason,
            "short_summary": self.short_summary,
            "penalty": self.penalty,
            "contributing_sources": list(self.contributing_sources),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Candidate":
        """Create a Candidate from a dict (e.g. collector output)."""
        return cls(
            title=d.get("title", ""),
            url=d.get("url", ""),
            source=d.get("source", ""),
            source_name=d.get("source_name", ""),
            source_type=d.get("source_type", d.get("source", "")),
            snippet=d.get("snippet"),
            published_at=d.get("published_at"),
            score=float(d.get("score") or 0.0),
            upvotes=d.get("upvotes"),
            comments=d.get("comments"),
            stars=d.get("stars"),
            forks=d.get("forks"),
            reposts=d.get("reposts"),
            upvote_ratio=d.get("upvote_ratio"),
            velocity=d.get("velocity"),
            category=d.get("category"),
            raw_text=d.get("raw_text"),
            extracted_text=d.get("extracted_text"),
            crosspost_count=int(d.get("crosspost_count") or 1),
            raw_json=d.get("raw_json"),
            candidate_id=d.get("candidate_id"),
            importance=d.get("importance"),
            reason=d.get("reason"),
            short_summary=d.get("short_summary"),
            penalty=float(d.get("penalty") or 1.0),
            contributing_sources=list(d.get("contributing_sources") or []),
        )


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


_KNOWN_CANDIDATE_FIELDS = frozenset({
    "title", "url", "source", "source_name", "source_type",
    "snippet", "published_at", "score", "upvotes", "comments",
    "stars", "forks", "reposts", "upvote_ratio", "velocity",
    "category", "raw_text", "extracted_text", "crosspost_count",
    "raw_json", "candidate_id", "importance", "reason",
    "short_summary", "penalty", "contributing_sources",
})


def new_candidate(
    *,
    title: str,
    url: str,
    source: str,
    source_name: str,
    **extra: Any,
) -> dict[str, Any]:
    """Build a Candidate dict with sane null defaults for every key.

    This is a backward-compatible factory that returns a dict.
    Extra fields are validated against known Candidate field names —
    typos produce a warning instead of being silently accepted.
    New code should use Candidate.from_dict() or the Candidate dataclass
    directly for type safety.
    """
    c = Candidate(
        title=title,
        url=url,
        source=source,
        source_name=source_name,
    )
    d = c.to_dict()
    # Apply extra fields with validation.
    for k, v in extra.items():
        if k not in _KNOWN_CANDIDATE_FIELDS:
            import logging
            logging.getLogger(__name__).warning(
                "new_candidate: unknown field %r — possible typo. Known: %s",
                k, ", ".join(sorted(_KNOWN_CANDIDATE_FIELDS)),
            )
        d[k] = v
    return d


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