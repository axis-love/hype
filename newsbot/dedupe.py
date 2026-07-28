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

Primary-source selection is deterministic regardless of collector order:
a pre-merge preference is computed from configured source weights and
engagement signals, so reversing the input order produces the same primary
source.
"""

from __future__ import annotations

import logging
import math
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
    "producthunt": 0.8,
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
    - The representative source is deterministic: highest pre-merge preference
      wins, tie-break by normalized source ID alphabetically. This is
      order-independent — reversing collector input produces the same primary.
    - When the primary changes, all representative fields (source, url, title,
      snippet, published_at) are copied consistently from the new primary.
    """
    # Track contributing sources in a persistent list.
    if not keep.get("contributing_sources"):
        keep["contributing_sources"] = [keep.get("source") or "unknown"]
    # Track per-source engagement values so same-source duplicates
    # can take MAX per source without inflating other sources' totals.
    if "_per_source_eng" not in keep:
        keep["_per_source_eng"] = {}
    my_source = keep.get("source") or "unknown"
    if my_source not in keep["_per_source_eng"]:
        keep["_per_source_eng"][my_source] = {
            f: keep.get(f) or 0 for f in ("upvotes", "comments", "stars", "forks", "reposts")
        }
    other_source = other.get("source") or "unknown"
    other_eng = {
        f: other.get(f) or 0 for f in ("upvotes", "comments", "stars", "forks", "reposts")
    }

    # Track the individual preference of the current primary candidate.
    if "_primary_preference" not in keep:
        keep["_primary_preference"] = _pre_merge_preference(keep)
    other_pref = _pre_merge_preference(other)

    if other_source not in keep["contributing_sources"]:
        # New distinct source: add to contributing sources, record its engagement.
        keep["contributing_sources"].append(other_source)
        keep["_per_source_eng"][other_source] = other_eng
    else:
        # Same-source duplicate: take MAX engagement per field for this source.
        if other_source not in keep["_per_source_eng"]:
            keep["_per_source_eng"][other_source] = {
                f: 0 for f in ("upvotes", "comments", "stars", "forks", "reposts")
            }
        for field in ("upvotes", "comments", "stars", "forks", "reposts"):
            keep["_per_source_eng"][other_source][field] = max(
                keep["_per_source_eng"][other_source][field],
                other_eng[field]
            )

    # Recompute total engagement from per-source tracking.
    for field in ("upvotes", "comments", "stars", "forks", "reposts"):
        keep[field] = sum(
            src_eng[field] for src_eng in keep["_per_source_eng"].values()
        )

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

    # Deterministic primary-source selection using pre-merge preference:
    # Compares individual candidate preferences (stored in _primary_preference),
    # not the inflated merged engagement value. Order-independent.
    # Tie-break: source ID alphabetically.
    if other_pref > keep["_primary_preference"] or (
        other_pref == keep["_primary_preference"]
        and str(other.get("source") or "") < str(keep.get("source") or "")
    ):
        # Switch primary: copy all representative fields from other.
        keep["source"] = other.get("source") or keep.get("source")
        keep["url"] = other.get("url") or keep.get("url")
        keep["title"] = other.get("title") or keep.get("title")
        if other.get("snippet"):
            keep["snippet"] = other["snippet"]
        if other.get("published_at"):
            keep["published_at"] = other["published_at"]
        if other.get("score") is not None:
            keep["score"] = other["score"]
        # Update primary preference to the new primary's individual value.
        keep["_primary_preference"] = other_pref

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
            # After merging, update indexes with the merged item's
            # canonical URL and normalized title so that future candidates
            # matching the merged item (or the item just absorbed) find
            # the same group. This prevents transitive duplicates from
            # splitting into separate groups.
            if canon:
                url_index[canon] = match_idx
            if norm_title:
                title_index[norm_title] = match_idx
            if gh_key:
                gh_index[gh_key] = match_idx

    log.info("dedupe: %d candidates -> %d unique", len(items), len(result))

    # Clean up internal-only tracking fields (but keep contributing_sources).
    for item in result:
        item.pop("_source_names_set", None)
        item.pop("_primary_preference", None)
        item.pop("_per_source_eng", None)

    return result