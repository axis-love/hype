"""Cross-source deduplication and merge.

Implements the architecture spec §9:

  - canonical URL match (strip tracking params, normalize host, preserve content-identifying params)
  - normalized lowercase title match
  - fuzzy title similarity > 0.90 (rapidfuzz)
  - same GitHub repo URL
  - trends containment: a trends candidate whose trend title's tokens
    (minus stopwords, ≥2 tokens) ALL appear in another candidate's title
    ⇒ same story → merge. Scoped to source == "trends".

When duplicates are found, **merge** their engagement signals instead of
dropping the weaker item: sum upvotes/comments/stars (only across distinct
sources), take the max published_at, and stamp crosspost_count = number of
distinct sources. The crosspost bonus (+30 in scoring) is one of the
strongest signals.

Primary-source selection is deterministic regardless of collector order:
a pre-merge preference is computed from configured source weights and
engagement signals, so reversing the input order produces the same primary
source.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

try:
    from rapidfuzz import fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    fuzz = None
    _HAS_RAPIDFUZZ = False

log = logging.getLogger(__name__)

FUZZY_THRESHOLD = 90.0  # rapidfuzz.fuzz.ratio is 0-100

# Query parameters that are tracking/referral and should be stripped.
# All others are preserved because they may identify distinct content.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "ref_url", "source", "utm", "mc_cid", "mc_eid",
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "sr", "sr_share",
    "spm", "scm", "campaign", "_hsenc", "_hsmi", "hsCtaTracking",
    "feature", "ocid", "ito", "cmpid", "src", "share",
})

# Default source weights for pre-merge ranking. Overridden by config at runtime
# via _set_pre_merge_weights(). This avoids a hard dependency on config in dedupe.
_PRE_MERGE_WEIGHTS: dict[str, float] = {
    "hackernews": 1.2,
    "hn": 1.2,
    "reddit": 1.0,
    "github": 1.1,
    "huggingface_papers": 1.2,
    "rss": 0.5,
}


def _set_pre_merge_weights(weights: dict[str, float]) -> None:
    """Override the default pre-merge weights with the active config values.

    Called by main.py after config is loaded so that pre-merge preference
    uses the operator's configured source weights, not hard-coded defaults.
    """
    global _PRE_MERGE_WEIGHTS
    _PRE_MERGE_WEIGHTS = dict(weights)

_SOURCE_ALIASES_PRE: dict[str, str] = {
    "hn": "hackernews",
}


# --- Trends containment dedupe rule (H-3) --------------------------------

# Stopwords removed from trend titles before token containment check.
# These are high-frequency tokens that dilute the containment signal.
_TRENDS_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "this", "that", "these",
    "those", "it", "its", "as", "if", "then", "than", "so", "not", "no",
    "vs", "new", "best", "top", "update", "updates",
})


def _trend_tokens(trend_title: str) -> list[str]:
    """Extract meaningful tokens from a trend title for containment check.

    Lowercases, splits on non-alphanumeric, drops stopwords, and returns
    the remaining tokens. Returns [] if fewer than 2 tokens survive — the
    containment rule requires ≥2 tokens to fire.
    """
    raw_tokens = re.findall(r"[a-z0-9]+", (trend_title or "").lower())
    tokens = [t for t in raw_tokens if t not in _TRENDS_STOPWORDS]
    return tokens


def _trends_containment_match(
    trends_item: dict[str, Any],
    other_item: dict[str, Any],
) -> bool:
    """Check if a trends candidate's trend tokens are ALL in another title.

    Returns True if the trend title (from source_name "trends/<title>")
    produces ≥2 non-stopword tokens and ALL of those tokens appear as
    WHOLE TOKENS in the other candidate's title. Scoped to
    source == "trends".

    Token-set containment (not substring): substring matching merged
    "GTA 6 leak" with GTA 5 articles ("6" in "2026", "ai" in "maintain").
    """
    # Only apply to trends candidates.
    if str(trends_item.get("source") or "") != "trends":
        return False

    # Extract trend title from source_name "trends/<title>".
    source_name = str(trends_item.get("source_name") or "")
    if not source_name.startswith("trends/"):
        return False
    trend_title = source_name[len("trends/"):]

    tokens = _trend_tokens(trend_title)
    if len(tokens) < 2:
        return False  # need at least 2 tokens for a meaningful match

    other_title = _normalize_title(other_item.get("title"))
    if not other_title:
        return False

    # ALL trend tokens must appear as whole tokens in the other title.
    # Headlines inflect ("leak" → "leaks"/"leaked"), so a trend token of
    # 4+ chars also matches a title token it prefixes. Short tokens and
    # digits stay exact — that is what keeps "6" off "2026" and "ai" off
    # "maintain".
    other_tokens = set(re.findall(r"[a-z0-9]+", other_title.lower()))
    return all(_trend_token_present(token, other_tokens) for token in tokens)


def _trend_token_present(token: str, other_tokens: set[str]) -> bool:
    """Whole-token match, with prefix matching for tokens of 4+ chars."""
    if token in other_tokens:
        return True
    return len(token) >= 4 and any(other.startswith(token) for other in other_tokens)



def _pre_merge_preference(item: dict[str, Any]) -> float:
    """Compute a deterministic pre-merge preference for primary-source selection.

    Uses configured source weights and engagement signals to rank candidates
    before scoring. This ensures the primary source is deterministic regardless
    of collector order, even when all candidates have equal default scores.

    Higher preference = more likely to be the primary representative.
    """
    src = str(item.get("source") or "").strip()
    src = _SOURCE_ALIASES_PRE.get(src, src)
    weight = _PRE_MERGE_WEIGHTS.get(src, 1.0)

    # RSS feed weight override.
    raw_json = item.get("raw_json")
    if isinstance(raw_json, dict):
        feed_weight = raw_json.get("weight")
        if feed_weight is not None:
            try:
                weight = float(feed_weight)
            except (TypeError, ValueError):
                pass

    # Engagement signal: log1p-weighted (same formula as scoring.py, minus recency).
    eng = (
        math.log1p(max(0, item.get("upvotes") or 0)) * 10.0
        + math.log1p(max(0, item.get("comments") or 0)) * 25.0
        + math.log1p(max(0, item.get("stars") or 0)) * 15.0
        + math.log1p(max(0, item.get("reposts") or 0)) * 20.0
    )

    # Preference = engagement * source_weight. Deterministic, order-independent.
    # If scores are set (rare at dedupe stage), use as a secondary signal.
    score = float(item.get("score") or 0.0)
    return eng * weight + score


def _canonical_url(url: Any) -> str:
    """Normalize a URL for dedup: lowercase host, strip scheme, drop tracking
    query params and fragment, but preserve content-identifying query params.

    Preserves original URL encoding (%xx, +) so signed URLs and encoded
    paths remain distinct. Tracking params are stripped by name only —
    the remaining query string is kept in its original encoded form.

    Examples:
      item?id=1 and item?id=2 → distinct canonical URLs (preserved)
      example.com/post?utm_source=x → example.com/post (tracking stripped)
    """
    s = str(url or "").strip()
    if not s:
        return ""
    try:
        parts = urlsplit(s)
    except ValueError:
        return ""
    host = (parts.netloc or "").lower()
    # Strip leading 'www.' for host comparison.
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"

    # Preserve query params that are NOT tracking params.
    # Keep the original encoded form — do not decode %xx or + via parse_qsl.
    query = parts.query
    if query:
        # Split into key=value pairs by & to check param names,
        # but preserve the original encoded values.
        kept = []
        for pair in query.split("&"):
            if not pair:
                continue
            # Extract just the key (before =) to check against tracking params.
            key = pair.split("=", 1)[0].lower()
            if key not in _TRACKING_PARAMS:
                kept.append(pair)
        if kept:
            # Sort for determinism (order-independent canonicalization).
            kept.sort()
            query = "&".join(kept)
        else:
            query = ""
    else:
        query = ""

    if query:
        return f"{host}{path}?{query}"
    return f"{host}{path}"


def _normalize_title(title: Any) -> str:
    return " ".join(str(title or "").lower().split())


def _external_url_key(item: dict[str, Any]) -> str:
    """Canonical key of the link a post points at (raw_json.external_url).

    Reddit link posts carry the permalink as ``url`` and the linked
    article in ``raw_json.external_url``. Self-posts point at
    themselves, which canonicalizes to the permalink — harmless, the key
    already exists. Returns "" when there is no usable external link.
    """
    raw_json = item.get("raw_json")
    if not isinstance(raw_json, dict):
        return ""
    external = str(raw_json.get("external_url") or "").strip()
    if not external.startswith(("http://", "https://")):
        return ""
    return _canonical_url(external)


def _github_repo_key(item: dict[str, Any]) -> str:
    """For GitHub candidates, the repo full_name is a stable identity."""
    if item.get("source") != "github":
        return ""
    raw = item.get("raw_json")
    if isinstance(raw, dict):
        full_name = str(raw.get("full_name") or "").strip().lower()
        if full_name:
            return full_name
    # Fall back to title (we set title=full_name in the collector).
    return str(item.get("title") or "").strip().lower()


def _row_github_repo_key(url: Any) -> str:
    """GitHub repo identity for a STORE ROW, derived from its URL.

    Store rows are medium-neutral and carry no `source` field, so
    `_github_repo_key` (candidate-side) cannot apply. Instead the row's URL
    is parsed: any github.com host (www./api./raw. etc.) with an
    owner/repo path yields the lowercased "owner/repo" key. Mirrors the
    candidate key: full_name is lowercased, so "Owner/Repo" == "owner/repo".
    Non-GitHub URLs and paths without a full owner/repo return "".
    """
    s = str(url or "").strip()
    if not s:
        return ""
    try:
        parts = urlsplit(s)
    except ValueError:
        return ""
    host = (parts.netloc or "").lower()
    host = host.removeprefix("www.")
    if host != "github.com" and not host.endswith(".github.com"):
        return ""
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) < 2:
        return ""
    owner, repo = segments[0], segments[1]
    repo = repo.removesuffix(".git")
    if not owner or not repo:
        return ""
    return f"{owner}/{repo}".lower()


def _merged_urls_list(row: dict[str, Any]) -> list[str]:
    """Parse a store row's merged_urls JSON string into a list of URLs.

    merged_urls is stored as a JSON list string (see db.merge_into_store_row).
    Malformed JSON, missing values, non-list payloads, and non-string entries
    are all tolerated — they yield an empty list so matching falls through
    to the other identity checks instead of crashing.
    """
    raw = row.get("merged_urls")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, str)]


def _row_raw_json(row: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a store row's raw_json (a JSON *string* in the DB) into a dict.

    Tolerant: str or dict input, malformed JSON, and missing values all
    degrade to None — never raises. Mirrors _merged_urls_list's tolerance
    so match_candidate_to_store can consult row-side external_url without
    a separate try/except at every call site.
    """
    raw = row.get("raw_json")
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        # json.loads can yield non-dict types (list, int, etc).
        return parsed if isinstance(parsed, dict) else None
    return None


def _row_external_url_key(row: dict[str, Any]) -> str:
    """Canonical key of the external article URL stored in a row's raw_json.

    Store rows carry raw_json as a JSON *string* (dict only in-memory).
    This helper parses it tolerantly (via _row_raw_json) and returns the
    canonical URL of the linked article, or "" when absent/malformed.

    Mirrors _external_url_key (candidate-side) so that a Reddit link post
    whose raw_json.external_url is apple.com can match a store row whose
    raw_json.external_url IS apple.com — cross-side identity.
    """
    rj = _row_raw_json(row)
    if rj is None:
        return ""
    external = str(rj.get("external_url") or "").strip()
    if not external.startswith(("http://", "https://")):
        return ""
    return _canonical_url(external)


def _fuzzy_ratio(a: str, b: str) -> float:
    """rapidfuzz ratio if available, else a cheap token-overlap fallback."""
    if _HAS_RAPIDFUZZ:
        return float(fuzz.ratio(a, b))
    # Fallback: token Jaccard. Crude but avoids a hard dep on rapidfuzz.
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    return 100.0 * len(ta & tb) / len(ta | tb)


_ENG_FIELDS = ("upvotes", "comments", "stars", "forks", "reposts")


def _eng_snapshot(item: dict[str, Any]) -> dict[str, int]:
    return {f: int(item.get(f) or 0) for f in _ENG_FIELDS}


@dataclass(slots=True)
class _MergeGroup:
    """Scratch accumulator for one duplicate group. Never leaks onto the dict."""

    rep: dict[str, Any]
    per_source_eng: dict[str, dict[str, int]]
    primary_pref: float
    merged_published_at: Any
    source_names: set[str]

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> _MergeGroup:
        src = item.get("source") or "unknown"
        if not item.get("contributing_sources"):
            item["contributing_sources"] = [src]
        if not item.get("contributing_urls"):
            item["contributing_urls"] = []
        names: set[str] = set()
        for n in str(item.get("source_name") or "").split(" + "):
            n = n.strip()
            if n:
                names.add(n)
        return cls(
            rep=item,
            per_source_eng={src: _eng_snapshot(item)},
            primary_pref=_pre_merge_preference(item),
            merged_published_at=item.get("published_at"),
            source_names=names,
        )

    def finalize(self) -> dict[str, Any]:
        keep = self.rep
        keep["source_name"] = " + ".join(sorted(self.source_names))
        for field in _ENG_FIELDS:
            keep[field] = sum(src_eng[field] for src_eng in self.per_source_eng.values())
        sources = set(keep["contributing_sources"])
        sources.discard(None)
        sources.discard("")
        sources.discard("unknown")
        keep["crosspost_count"] = max(int(keep.get("crosspost_count") or 1), len(sources))
        if self.merged_published_at:
            keep["published_at"] = self.merged_published_at
        return keep


def _merge_pair(group: _MergeGroup, other: dict[str, Any]) -> _MergeGroup:
    """Merge *other* into *group*, summing engagement across distinct sources."""
    keep = group.rep
    other_source = other.get("source") or "unknown"
    other_eng = _eng_snapshot(other)
    other_pref = _pre_merge_preference(other)

    for curl in (other.get("url"), _external_url_key(other)):
        cs = str(curl or "").strip()
        if cs and cs not in keep["contributing_urls"]:
            keep["contributing_urls"].append(cs)

    if other_source not in keep["contributing_sources"]:
        keep["contributing_sources"].append(other_source)
        group.per_source_eng[other_source] = other_eng
    else:
        if other_source not in group.per_source_eng:
            group.per_source_eng[other_source] = {f: 0 for f in _ENG_FIELDS}
        for fld in _ENG_FIELDS:
            group.per_source_eng[other_source][fld] = max(
                group.per_source_eng[other_source][fld], other_eng[fld]
            )

    for fld in _ENG_FIELDS:
        keep[fld] = sum(src_eng[fld] for src_eng in group.per_source_eng.values())

    if other.get("upvote_ratio") is not None:
        a = keep.get("upvote_ratio") or 0.0
        b = other.get("upvote_ratio") or 0.0
        keep["upvote_ratio"] = max(a, b)

    a_ts = group.merged_published_at or keep.get("published_at")
    b_ts = other.get("published_at")
    if a_ts and b_ts:
        group.merged_published_at = max(str(a_ts), str(b_ts))
    elif b_ts and not a_ts:
        group.merged_published_at = b_ts
    keep["published_at"] = group.merged_published_at

    if len(str(other.get("snippet") or "")) > len(str(keep.get("snippet") or "")):
        keep["snippet"] = other.get("snippet")

    for it in (keep, other):
        for n in str(it.get("source_name") or "").split(" + "):
            n = n.strip()
            if n:
                group.source_names.add(n)
    keep["source_name"] = " + ".join(sorted(group.source_names))

    sources = set(keep["contributing_sources"])
    sources.discard(None)
    sources.discard("")
    sources.discard("unknown")
    keep["crosspost_count"] = max(int(keep.get("crosspost_count") or 1), len(sources))

    if other_pref > group.primary_pref or (
        other_pref == group.primary_pref
        and str(other.get("source") or "") < str(keep.get("source") or "")
    ):
        for curl in (keep.get("url"), _external_url_key(keep)):
            cs = str(curl or "").strip()
            if cs and cs not in keep["contributing_urls"]:
                keep["contributing_urls"].append(cs)
        keep["source"] = other.get("source") or keep.get("source")
        keep["url"] = other.get("url") or keep.get("url")
        keep["title"] = other.get("title") or keep.get("title")
        old_rj = keep.get("raw_json")
        new_rj = other.get("raw_json")
        if isinstance(new_rj, dict):
            keep["raw_json"] = {**(old_rj if isinstance(old_rj, dict) else {}), **new_rj}
        if other.get("snippet"):
            keep["snippet"] = other["snippet"]
        keep["published_at"] = group.merged_published_at
        if other.get("score") is not None:
            keep["score"] = other["score"]
        group.primary_pref = other_pref
    return group


def dedupe_and_merge(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group duplicates and merge each group into one candidate."""
    if not items:
        return []

    result: list[_MergeGroup] = []
    url_index: dict[str, int] = {}
    title_index: dict[str, int] = {}
    gh_index: dict[str, int] = {}

    for item in items:
        canon = _canonical_url(item.get("url"))
        external = _external_url_key(item)
        norm_title = _normalize_title(item.get("title"))
        gh_key = _github_repo_key(item)
        match_idx: int | None = None

        if gh_key and gh_key in gh_index:
            match_idx = gh_index[gh_key]
        elif canon and canon in url_index:
            match_idx = url_index[canon]
        elif external and external in url_index:
            match_idx = url_index[external]
        elif norm_title and norm_title in title_index:
            match_idx = title_index[norm_title]
        else:
            if str(item.get("source") or "") == "trends":
                for idx, group in enumerate(result):
                    if _trends_containment_match(item, group.rep):
                        match_idx = idx
                        log.info(
                            "dedupe_trends_match: trend '%s' matched candidate '%s'",
                            str(item.get("source_name") or ""),
                            str(group.rep.get("title") or "")[:80],
                        )
                        break
            if match_idx is None and norm_title:
                best_idx = -1
                best_ratio = 0.0
                for idx, group in enumerate(result):
                    rep_title = _normalize_title(group.rep.get("title"))
                    if not rep_title:
                        continue
                    ratio = _fuzzy_ratio(norm_title, rep_title)
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_idx = idx
                    if best_ratio >= FUZZY_THRESHOLD:
                        break
                if best_idx >= 0 and best_ratio >= FUZZY_THRESHOLD:
                    match_idx = best_idx

        if match_idx is None:
            result.append(_MergeGroup.from_item(item))
            idx = len(result) - 1
            if canon:
                url_index[canon] = idx
            if external:
                url_index[external] = idx
            if norm_title:
                title_index[norm_title] = idx
            if gh_key:
                gh_index[gh_key] = idx
        else:
            _merge_pair(result[match_idx], item)
            if canon:
                url_index[canon] = match_idx
            if external:
                url_index[external] = match_idx
            if norm_title:
                title_index[norm_title] = match_idx
            if gh_key:
                gh_index[gh_key] = match_idx

    log.info("dedupe: %d candidates -> %d unique", len(items), len(result))
    return [g.finalize() for g in result]


@dataclass(frozen=True, slots=True)
class _RowKeys:
    row: dict[str, Any]
    canon: str
    url_set: frozenset[str]
    ext_key: str
    gh_key: str
    norm_title: str


def match_candidate_to_store(candidate: dict, store_rows: list[dict]) -> dict | None:
    """Return the store row matching this candidate, or None."""
    if not store_rows:
        return None

    indexed: list[_RowKeys] = []
    for row in store_rows:
        row_canon = _canonical_url(row.get("url"))
        merged = [_canonical_url(u) for u in _merged_urls_list(row)]
        indexed.append(
            _RowKeys(
                row=row,
                canon=row_canon,
                url_set=frozenset(x for x in [row_canon, *merged] if x),
                ext_key=_row_external_url_key(row),
                gh_key=_row_github_repo_key(row.get("url")),
                norm_title=_normalize_title(row.get("title")),
            )
        )

    gh_key = _github_repo_key(candidate)
    if gh_key:
        for keys in indexed:
            if gh_key == keys.gh_key:
                return keys.row

    canon = _canonical_url(candidate.get("url"))
    ext_key = _external_url_key(candidate)
    if canon or ext_key:
        for keys in indexed:
            if canon and canon in keys.url_set:
                return keys.row
            if ext_key and ext_key in keys.url_set:
                return keys.row
            if canon and keys.ext_key and canon == keys.ext_key:
                return keys.row
            if ext_key and keys.ext_key and ext_key == keys.ext_key:
                return keys.row

    norm_title = _normalize_title(candidate.get("title"))
    if norm_title:
        for keys in indexed:
            if norm_title == keys.norm_title:
                return keys.row
        best_row: dict | None = None
        best_ratio = 0.0
        for keys in indexed:
            if not keys.norm_title:
                continue
            ratio = _fuzzy_ratio(norm_title, keys.norm_title)
            if ratio > best_ratio:
                best_ratio = ratio
                best_row = keys.row
            if best_ratio >= FUZZY_THRESHOLD:
                break
        if best_row is not None and best_ratio >= FUZZY_THRESHOLD:
            return best_row
    return None
