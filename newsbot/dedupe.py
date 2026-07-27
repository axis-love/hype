"""Cross-source deduplication and merge.

Implements the architecture spec §9:

  - canonical URL match (strip tracking params, normalize host, preserve content-identifying params)
  - normalized lowercase title match
  - fuzzy title similarity > 0.90 (rapidfuzz)
  - same GitHub repo URL

When duplicates are found, **merge** their engagement signals instead of
dropping the weaker item: sum upvotes/comments/stars (only across distinct
sources), take the max published_at, and stamp crosspost_count = number of
distinct sources. The crosspost bonus (+30 in scoring) is one of the
strongest signals.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit, parse_qsl, urlencode

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


def _canonical_url(url: Any) -> str:
    """Normalize a URL for dedup: lowercase host, strip scheme, drop tracking
    query params and fragment, but preserve content-identifying query params.

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
    query = parts.query
    if query:
        kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
                if k.lower() not in _TRACKING_PARAMS]
        if kept:
            # Sort for determinism (order-independent canonicalization).
            kept.sort()
            # Use urlencode for proper URL encoding of values.
            query = urlencode(kept)
        else:
            query = ""
    else:
        query = ""

    if query:
        return f"{host}{path}?{query}"
    return f"{host}{path}"


def _normalize_title(title: Any) -> str:
    return " ".join(str(title or "").lower().split())


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


def _merge_pair(keep: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """Merge *other* into *keep*, summing engagement across distinct sources only.

    Returns the merged *keep* dict (mutated in place).

    Key rules:
    - Engagement is summed only when the other item comes from a different
      source than any already merged. Same-source duplicates (e.g. same GitHub
      repo from multiple search queries) take the MAX engagement value
      (not first-seen, not re-summed).
    - crosspost_count reflects all distinct contributing sources (not capped at 2).
    - contributing_sources is a persistent list on the output item.
    - The representative source is deterministic: highest score wins, tie-break
      by source name alphabetically. Since dedupe runs before scoring, the
      first-seen item is the default primary (stable, order-independent for
      same-input runs).
    """
    # Track contributing sources in a persistent list.
    if "contributing_sources" not in keep:
        keep["contributing_sources"] = [keep.get("source") or "unknown"]
    other_source = other.get("source") or "unknown"

    # Only sum engagement if this is from a new source (prevents inflation).
    if other_source not in keep["contributing_sources"]:
        keep["contributing_sources"].append(other_source)
        for field in ("upvotes", "comments", "stars", "forks", "reposts"):
            a = keep.get(field) or 0
            b = other.get(field) or 0
            if a or b:
                keep[field] = (a or 0) + (b or 0)
    else:
        # Same-source duplicate: take max engagement (not first-seen).
        for field in ("upvotes", "comments", "stars", "forks", "reposts"):
            a = keep.get(field) or 0
            b = other.get(field) or 0
            if b > a:
                keep[field] = b

    # upvote_ratio: take the max (Reddit-only field; non-Reddit items have None).
    if other.get("upvote_ratio") is not None:
        a = keep.get("upvote_ratio") or 0.0
        b = other.get("upvote_ratio") or 0.0
        keep["upvote_ratio"] = max(a, b)

    # published_at: keep the most recent (max).
    a_ts = keep.get("published_at")
    b_ts = other.get("published_at")
    if a_ts and b_ts:
        keep["published_at"] = max(str(a_ts), str(b_ts))
    elif b_ts and not a_ts:
        keep["published_at"] = b_ts

    # snippet: keep the longer one (more info).
    if len(str(other.get("snippet") or "")) > len(str(keep.get("snippet") or "")):
        keep["snippet"] = other.get("snippet")

    # Track the union of source names so the digest can list them.
    if "_source_names_set" not in keep:
        keep["_source_names_set"] = set()
    for it in (keep, other):
        for n in str(it.get("source_name") or "").split(" + "):
            n = n.strip()
            if n:
                keep["_source_names_set"].add(n)
    keep["source_name"] = " + ".join(sorted(keep["_source_names_set"]))

    # crosspost_count = number of distinct contributing sources (not capped at 2).
    sources = set(keep["contributing_sources"])
    sources.discard(None)
    sources.discard("")
    sources.discard("unknown")
    keep["crosspost_count"] = max(int(keep.get("crosspost_count") or 1), len(sources))

    # Deterministic primary source selection:
    # Since dedupe runs BEFORE scoring, candidates have equal/default scores.
    # Tie-break: keep the first-seen item's source (stable, deterministic for
    # same input order). If other has a higher score, switch.
    if other.get("score") is not None:
        if keep.get("score") is None or float(other["score"]) > float(keep["score"]):
            keep["source"] = other_source

    return keep


def dedupe_and_merge(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group duplicates and merge each group into one candidate.

    Returns the deduplicated list (order preserved by first occurrence).
    """
    if not items:
        return []

    # Index: canonical_key -> index of the representative in `result`.
    result: list[dict[str, Any]] = []
    url_index: dict[str, int] = {}
    title_index: dict[str, int] = {}
    gh_index: dict[str, int] = {}

    for item in items:
        canon = _canonical_url(item.get("url"))
        norm_title = _normalize_title(item.get("title"))
        gh_key = _github_repo_key(item)

        match_idx: int | None = None

        # 1. GitHub repo full_name (strongest for GitHub items).
        if gh_key and gh_key in gh_index:
            match_idx = gh_index[gh_key]
        # 2. Canonical URL.
        elif canon and canon in url_index:
            match_idx = url_index[canon]
        # 3. Exact normalized title.
        elif norm_title and norm_title in title_index:
            match_idx = title_index[norm_title]
        else:
            # 4. Fuzzy title match (>0.90). Linear scan — N is small (hundreds).
            if norm_title:
                best_idx = -1
                best_ratio = 0.0
                for idx, rep in enumerate(result):
                    rep_title = _normalize_title(rep.get("title"))
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
            result.append(item)
            idx = len(result) - 1
            if canon:
                url_index[canon] = idx
            if norm_title:
                title_index[norm_title] = idx
            if gh_key:
                gh_index[gh_key] = idx
        else:
            _merge_pair(result[match_idx], item)

    log.info("dedupe: %d candidates -> %d unique", len(items), len(result))

    # Clean up internal-only tracking fields (but keep contributing_sources).
    for item in result:
        item.pop("_source_names_set", None)

    return result