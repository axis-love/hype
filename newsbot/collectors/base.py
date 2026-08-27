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
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


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
#: Used by Candidate so collectors can emit either form (e.g. "hackernews"
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
    source: str          # 'hn' | 'reddit' | 'github' | 'rss' | etc.
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
    contributing_urls: list[str] = field(default_factory=list, repr=False)
    _source_names_set: set[str] = field(default_factory=set, repr=False)

    # Scoring breakdown (filled by scoring.score_all, used by /scores command).
    score_breakdown: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        """Validate required fields and engagement values at construction time."""
        if not self.title:
            raise ValueError("Candidate requires a non-empty title")
        if not self.source:
            raise ValueError("Candidate requires a non-empty source")
        # Validate and normalize source ID.
        self.source = _normalize_source_id(self.source)
        if not self.source_name:
            raise ValueError("Candidate requires a non-empty source_name")
        if not self.url:
            raise ValueError("Candidate requires a non-empty url")
        # Validate URL scheme.
        url_str = str(self.url).strip()
        if not url_str:
            raise ValueError("Candidate requires a non-empty url")
        if not (url_str.startswith("http://") or url_str.startswith("https://")):
            raise ValueError(
                f"Candidate.url must have http:// or https:// scheme, got {url_str!r}"
            )
        if not self.source_type:
            self.source_type = self.source
        # Validate engagement types: reject strings, booleans, NaN, infinity.
        for fname in ("upvotes", "comments", "stars", "forks", "reposts",
                      "crosspost_count"):
            val = getattr(self, fname)
            if val is not None:
                # Reject booleans (isinstance(True, int) is True in Python).
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
        # Validate engagement values are non-negative (if provided).
        for fname in ("upvotes", "comments", "stars", "forks", "reposts"):
            val = getattr(self, fname)
            if val is not None and val < 0:
                raise ValueError(f"Candidate.{fname} must be non-negative, got {val}")
        # Validate score and penalty.
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ValueError(f"Candidate.score must be numeric, got {type(self.score).__name__}")
        if isinstance(self.score, float) and (math.isnan(self.score) or math.isinf(self.score)):
            raise ValueError(f"Candidate.score must be finite, got {self.score}")
        if self.score < 0:
            raise ValueError(f"Candidate.score must be non-negative, got {self.score}")
        if isinstance(self.penalty, bool) or not isinstance(self.penalty, (int, float)):
            raise ValueError(f"Candidate.penalty must be numeric, got {type(self.penalty).__name__}")
        if isinstance(self.penalty, float) and (math.isnan(self.penalty) or math.isinf(self.penalty)):
            raise ValueError(f"Candidate.penalty must be finite, got {self.penalty}")
        if self.penalty < 0:
            raise ValueError(f"Candidate.penalty must be non-negative, got {self.penalty}")
        # Validate upvote_ratio.
        if self.upvote_ratio is not None:
            if isinstance(self.upvote_ratio, bool) or not isinstance(self.upvote_ratio, (int, float)):
                raise ValueError(f"Candidate.upvote_ratio must be numeric, got {type(self.upvote_ratio).__name__}")
            if not (0.0 <= float(self.upvote_ratio) <= 1.0):
                raise ValueError(f"Candidate.upvote_ratio must be in [0,1], got {self.upvote_ratio}")
        # Validate timestamp format (if provided).
        if self.published_at is not None:
            ts = str(self.published_at).strip()
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

    # --- dict-like compatibility for downstream code ---

    def __getitem__(self, key: str) -> Any:
        """Dict-like access for backward compatibility.

        Checks instance attributes first (including internal tracking fields
        set by dedupe), then falls back to to_dict().
        """
        # Check instance attributes first (covers internal fields like
        # _source_names_set, _primary_preference, _per_source_eng).
        if hasattr(self, key):
            return getattr(self, key)
        # Fall back to dict for known fields.
        d = self.to_dict()
        if key in d:
            return d[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-like .get() for backward compatibility.

        Checks instance attributes first, then to_dict().
        """
        if hasattr(self, key):
            return getattr(self, key)
        d = self.to_dict()
        return d.get(key, default)

    def __contains__(self, key: str) -> bool:
        """Dict-like 'in' check for backward compatibility.

        Checks instance attributes first (covers internal tracking fields
        set by dedupe like _per_source_eng, _primary_preference), then
        falls back to to_dict() for known dataclass fields.
        """
        if hasattr(self, key):
            return True
        return key in self.to_dict()

    def __setitem__(self, key: str, value: Any) -> None:
        """Dict-like assignment for backward compatibility with mutation code.

        Sets the attribute on the dataclass instance. Rejects unknown
        fields to catch typos — the same validation as from_dict.
        Allows internal tracking fields used by dedupe (prefixed with _).
        """
        if not isinstance(key, str):
            raise TypeError(f"Candidate key must be string, got {type(key).__name__}")
        # Allow internal dedupe tracking fields (prefixed with _).
        if key.startswith("_"):
            setattr(self, key, value)
            return
        if key not in _KNOWN_CANDIDATE_FIELDS:
            raise ValueError(
                f"Cannot set unknown Candidate field {key!r} — possible typo. "
                f"Known: {', '.join(sorted(_KNOWN_CANDIDATE_FIELDS))}"
            )
        setattr(self, key, value)

    def keys(self) -> list[str]:
        """Return keys for dict() compatibility.

        This makes dict(candidate) work correctly — Python's dict() constructor
        calls keys() then __getitem__ for each key.
        """
        return list(_KNOWN_CANDIDATE_FIELDS)

    def pop(self, key: str, default: Any = None) -> Any:
        """Dict-like pop for backward compatibility.

        Returns the attribute value and deletes it from the instance.
        """
        if hasattr(self, key):
            val = getattr(self, key)
            try:
                delattr(self, key)
            except AttributeError:
                pass
            return val
        return default

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dict compatible with existing pipeline code.

        Returns fresh copies of mutable fields (raw_json, contributing_sources)
        so callers can't mutate the Candidate's internal state via the dict.
        """
        import copy
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
            "raw_json": copy.deepcopy(self.raw_json) if self.raw_json else None,
            "candidate_id": self.candidate_id,
            "importance": self.importance,
            "reason": self.reason,
            "short_summary": self.short_summary,
            "penalty": self.penalty,
            "contributing_sources": list(self.contributing_sources),
            "contributing_urls": list(self.contributing_urls),
            "score_breakdown": dict(self.score_breakdown) if self.score_breakdown else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | "Candidate") -> "Candidate":
        """Create a Candidate from a dict or another Candidate.

        Rejects unknown fields — typos and invalid keys raise ValueError.
        Accepts both plain dicts and Candidate instances (via to_dict()).
        """
        if isinstance(d, Candidate):
            d = d.to_dict()
        unknown = set(d.keys()) - _KNOWN_CANDIDATE_FIELDS
        if unknown:
            raise ValueError(
                f"Unknown Candidate fields: {sorted(unknown)}. "
                f"Known: {sorted(_KNOWN_CANDIDATE_FIELDS)}"
            )

        def _numeric_or_none(val: Any, field_name: str) -> Any:
            """Extract numeric value or None, rejecting strings and booleans."""
            if val is None:
                return None
            if isinstance(val, bool):
                raise ValueError(f"Candidate.{field_name} must be numeric, got bool: {val}")
            if isinstance(val, (int, float)):
                if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                    raise ValueError(f"Candidate.{field_name} must be finite, got {val}")
                return val
            # Reject strings that look numeric — require actual int/float.
            raise ValueError(
                f"Candidate.{field_name} must be numeric, got {type(val).__name__}: {val!r}"
            )

        def _float_or_none(val: Any, field_name: str) -> Any:
            """Extract float or None."""
            if val is None:
                return None
            if isinstance(val, bool):
                raise ValueError(f"Candidate.{field_name} must be numeric, got bool: {val}")
            if isinstance(val, (int, float)):
                if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                    raise ValueError(f"Candidate.{field_name} must be finite, got {val}")
                return float(val)
            raise ValueError(
                f"Candidate.{field_name} must be numeric, got {type(val).__name__}: {val!r}"
            )

        return cls(
            title=d.get("title", ""),
            url=d.get("url", ""),
            source=d.get("source", ""),
            source_name=d.get("source_name", ""),
            source_type=d.get("source_type", d.get("source", "")),
            snippet=d.get("snippet"),
            published_at=d.get("published_at"),
            score=_float_or_none(d.get("score"), "score") if d.get("score") is not None else 0.0,
            upvotes=_numeric_or_none(d.get("upvotes"), "upvotes"),
            comments=_numeric_or_none(d.get("comments"), "comments"),
            stars=_numeric_or_none(d.get("stars"), "stars"),
            forks=_numeric_or_none(d.get("forks"), "forks"),
            reposts=_numeric_or_none(d.get("reposts"), "reposts"),
            upvote_ratio=_float_or_none(d.get("upvote_ratio"), "upvote_ratio"),
            velocity=_float_or_none(d.get("velocity"), "velocity"),
            category=d.get("category"),
            raw_text=d.get("raw_text"),
            extracted_text=d.get("extracted_text"),
            crosspost_count=int(_numeric_or_none(d.get("crosspost_count"), "crosspost_count")) if d.get("crosspost_count") is not None else 1,
            raw_json=d.get("raw_json"),
            candidate_id=d.get("candidate_id"),
            importance=_numeric_or_none(d.get("importance"), "importance"),
            reason=d.get("reason"),
            short_summary=d.get("short_summary"),
            penalty=_float_or_none(d.get("penalty"), "penalty") if d.get("penalty") is not None else 1.0,
            contributing_sources=list(d.get("contributing_sources") or []),
            contributing_urls=list(d.get("contributing_urls") or []),
            score_breakdown=d.get("score_breakdown"),
        )


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


_KNOWN_CANDIDATE_FIELDS = frozenset({
    "title", "url", "source", "source_name", "source_type",
    "snippet", "published_at", "score", "score_breakdown", "upvotes", "comments",
    "stars", "forks", "reposts", "upvote_ratio", "velocity",
    "category", "raw_text", "extracted_text", "crosspost_count",
    "raw_json", "candidate_id", "importance", "reason",
    "short_summary", "penalty", "contributing_sources", "contributing_urls",
})


def new_candidate(
    *,
    title: str,
    url: str,
    source: str,
    source_name: str,
    **extra: Any,
) -> Candidate:
    """Build a validated Candidate instance.

    Returns a Candidate (not a dict). The Candidate supports dict-like
    access via __getitem__ and .get() for backward compatibility.

    Unknown fields raise ValueError — typos are caught at construction.
    Invalid engagement values raise ValueError — no catch-and-continue.
    """
    # Build kwargs for Candidate constructor, filtering to known fields.
    known_extra = {}
    for k, v in extra.items():
        if k not in _KNOWN_CANDIDATE_FIELDS:
            raise ValueError(
                f"new_candidate: unknown field {k!r} — possible typo. "
                f"Known: {', '.join(sorted(_KNOWN_CANDIDATE_FIELDS))}"
            )
        known_extra[k] = v

    return Candidate(
        title=title,
        url=url,
        source=source,
        source_name=source_name,
        **known_extra,
    )


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