"""Tests for newsbot/dedupe.py — canonical URL, title fuzzy, GitHub repo, merge."""

import json

from newsbot.collectors.base import new_candidate
from newsbot.dedupe import (
    FUZZY_THRESHOLD,
    _canonical_url,
    _merge_pair,
    _normalize_title,
    dedupe_and_merge,
    match_candidate_to_store,
)


def test_canonical_url_strips_query_and_normalizes_host():
    assert _canonical_url("https://www.example.com/path?utm=1#frag") == "example.com/path"
    assert _canonical_url("HTTPS://Example.COM/Path/") == "example.com/Path"
    assert _canonical_url("") == ""
    assert _canonical_url(None) == ""


def test_normalize_title_lowercases_and_collapses_whitespace():
    assert _normalize_title("  Hello   World  ") == "hello world"
    assert _normalize_title("MixedCase") == "mixedcase"


def test_dedupe_merges_same_canonical_url_across_sources():
    a = new_candidate(title="Tool X released", url="https://example.com/post?ref=hn",
                      source="hn", source_name="Hacker News", upvotes=430, comments=180)
    b = new_candidate(title="Tool X released", url="https://example.com/post?ref=rd",
                      source="reddit", source_name="r/LocalLLaMA", upvotes=2100, comments=340)
    out = dedupe_and_merge([a, b])
    assert len(out) == 1
    merged = out[0]
    # Engagement summed across sources.
    assert merged["upvotes"] == 430 + 2100
    assert merged["comments"] == 180 + 340
    assert merged["crosspost_count"] == 2
    # Source names unioned.
    assert "Hacker News" in merged["source_name"]
    assert "r/LocalLLaMA" in merged["source_name"]


def test_dedupe_fuzzy_title_merges_near_duplicates():
    a = new_candidate(title="New local LLM tool is exploding on GitHub",
                      url="https://hn.algolia.com/x", source="hn", source_name="HN", upvotes=100)
    b = new_candidate(title="New local LLM tool is exploding on Github",
                      url="https://reddit.com/y", source="reddit", source_name="r/LocalLLaMA", upvotes=200)
    out = dedupe_and_merge([a, b])
    # Fuzzy match (>0.90) should merge these.
    assert len(out) == 1
    assert out[0]["crosspost_count"] == 2


def test_dedupe_github_repo_key_merges_same_full_name():
    a = new_candidate(title="owner/repo", url="https://github.com/owner/repo",
                      source="github", source_name="GitHub Trending", stars=8000)
    b = new_candidate(title="owner/repo", url="https://news.ycombinator.com/item?id=1",
                      source="hn", source_name="Hacker News", upvotes=430)
    out = dedupe_and_merge([a, b])
    # The GitHub repo full_name (from title) is the dedup key for github items,
    # but b's source is 'hn' so its _github_repo_key is "". They won't match on
    # canonical URL either. The exact-title match should merge them.
    assert len(out) == 1
    assert out[0]["stars"] == 8000
    assert out[0]["upvotes"] == 430
    assert out[0]["crosspost_count"] == 2


def test_dedupe_keeps_unrelated_items_separate():
    a = new_candidate(title="AI paper A", url="https://a.com", source="hn", source_name="HN")
    b = new_candidate(title="Game engine B", url="https://b.com", source="reddit", source_name="r/gamedev")
    out = dedupe_and_merge([a, b])
    assert len(out) == 2


def test_merge_pair_takes_longer_snippet():
    keep = new_candidate(title="t", url="https://example.com", source="hn", source_name="HN", snippet="short")
    other = new_candidate(title="t", url="https://example.com", source="reddit", source_name="r/x", snippet="a much longer snippet than the first one")
    _merge_pair(keep, other)
    assert "much longer snippet" in keep["snippet"]


# --- New tests for flow_001027 ------------------------------------------

def test_canonical_url_preserves_content_identifying_params():
    """item?id=1 and item?id=2 must be distinct canonical URLs."""
    assert _canonical_url("https://news.ycombinator.com/item?id=1") != _canonical_url("https://news.ycombinator.com/item?id=2")
    assert _canonical_url("https://example.com/article?id=42") == "example.com/article?id=42"


def test_canonical_url_strips_tracking_params():
    """utm_source, ref, fbclid etc. should be stripped."""
    assert _canonical_url("https://example.com/post?utm_source=x&utm_medium=y") == "example.com/post"
    assert _canonical_url("https://example.com/post?ref=hn") == "example.com/post"
    assert _canonical_url("https://example.com/post?fbclid=abc123") == "example.com/post"


def test_canonical_url_preserves_id_and_strips_tracking():
    """Mixed tracking + content params: only content params survive."""
    assert _canonical_url("https://example.com/article?id=42&utm_source=x") == "example.com/article?id=42"


def test_dedupe_same_source_does_not_inflate_engagement():
    """Same-source duplicates (e.g. GitHub from multiple search queries) should NOT sum engagement."""
    a = new_candidate(title="owner/repo", url="https://github.com/owner/repo",
                      source="github", source_name="GitHub Trending", stars=8000)
    b = new_candidate(title="owner/repo", url="https://github.com/owner/repo",
                      source="github", source_name="GitHub Trending", stars=8000)
    out = dedupe_and_merge([a, b])
    assert len(out) == 1
    assert out[0]["stars"] == 8000  # NOT 16000


def test_dedupe_three_sources_crosspost_count():
    """Three distinct sources should produce crosspost_count == 3."""
    a = new_candidate(title="Same Story", url="https://example.com/story",
                      source="hn", source_name="Hacker News", upvotes=100)
    b = new_candidate(title="Same Story", url="https://example.com/story",
                      source="reddit", source_name="r/LocalLLaMA", upvotes=200)
    c = new_candidate(title="Same Story", url="https://example.com/story",
                      source="github", source_name="GitHub Trending", stars=500)
    out = dedupe_and_merge([a, b, c])
    assert len(out) == 1
    assert out[0]["crosspost_count"] == 3


def test_dedupe_source_order_independence():
    """Primary source should be deterministic regardless of collector order.

    Uses unscored candidates (as in production) — pre-merge preference
    uses source weights and engagement, not scores.
    """
    # Same item from different sources, different order, NO scores set.
    a1 = new_candidate(title="Story X", url="https://example.com/x",
                       source="hn", source_name="HN", upvotes=100)
    a2 = new_candidate(title="Story X", url="https://example.com/x",
                       source="reddit", source_name="Reddit", upvotes=200)

    b1 = new_candidate(title="Story X", url="https://example.com/x",
                       source="reddit", source_name="Reddit", upvotes=200)
    b2 = new_candidate(title="Story X", url="https://example.com/x",
                       source="hn", source_name="HN", upvotes=100)

    out1 = dedupe_and_merge([a1, a2])
    out2 = dedupe_and_merge([b1, b2])
    assert len(out1) == 1
    assert len(out2) == 1
    # Primary source should be the same regardless of input order.
    # HN has weight 1.2, Reddit has weight 1.0. HN upvotes=100, Reddit upvotes=200.
    # HN preference: log1p(100)*10*1.2 = 4.62*10*1.2 = 55.4
    # Reddit preference: log1p(200)*10*1.0 = 5.30*10*1.0 = 53.0
    # HN wins due to higher source weight despite lower engagement.
    assert out1[0]["source"] == out2[0]["source"]
    # The primary should be HN (higher source weight).
    assert out1[0]["source"] == "hn"


def test_dedupe_source_order_independence_equal_engagement():
    """Equal engagement across sources — tie-break by source ID alphabetically."""
    a1 = new_candidate(title="Story Y", url="https://example.com/y",
                       source="hn", source_name="HN", upvotes=100)
    a2 = new_candidate(title="Story Y", url="https://example.com/y",
                       source="github", source_name="GitHub", upvotes=100)

    b1 = new_candidate(title="Story Y", url="https://example.com/y",
                       source="github", source_name="GitHub", upvotes=100)
    b2 = new_candidate(title="Story Y", url="https://example.com/y",
                       source="hn", source_name="HN", upvotes=100)

    out1 = dedupe_and_merge([a1, a2])
    out2 = dedupe_and_merge([b1, b2])
    assert len(out1) == 1
    assert len(out2) == 1
    # Same primary regardless of order. Tie-break: alphabetical source ID.
    # HN preference: log1p(100)*10*1.2 = 55.4
    # GitHub preference: log1p(100)*10*1.1 = 50.8
    # HN wins (higher weight). Both should pick HN.
    assert out1[0]["source"] == out2[0]["source"]


def test_dedupe_contributing_sources_set():
    """Merged candidate should track all contributing source names."""
    a = new_candidate(title="Story", url="https://example.com/s",
                      source="hn", source_name="Hacker News")
    b = new_candidate(title="Story", url="https://example.com/s",
                      source="reddit", source_name="r/LocalLLaMA")
    c = new_candidate(title="Story", url="https://example.com/s",
                      source="producthunt", source_name="Product Hunt")
    out = dedupe_and_merge([a, b, c])
    assert len(out) == 1
    merged = out[0]
    assert "Hacker News" in merged["source_name"]
    assert "r/LocalLLaMA" in merged["source_name"]
    assert "Product Hunt" in merged["source_name"]
    # Internal-only tracking field should be cleaned up.
    assert "_source_names_set" not in merged
    # contributing_sources is a persistent field (not deleted).
    assert "contributing_sources" in merged
    assert set(merged["contributing_sources"]) == {"hn", "reddit", "producthunt"}


def test_dedupe_same_source_takes_max_engagement():
    """Same-source duplicates (e.g. GitHub from multiple queries) take max, not sum."""
    a = new_candidate(title="Popular Repo", url="https://github.com/x/y",
                      source="github", source_name="GitHub", stars=1000, forks=100)
    b = new_candidate(title="Popular Repo", url="https://github.com/x/y",
                      source="github", source_name="GitHub", stars=1500, forks=80)
    out = dedupe_and_merge([a, b])
    assert len(out) == 1
    merged = out[0]
    # Same source: take max, not sum (would be 2500 if summed)
    assert merged["stars"] == 1500
    assert merged["forks"] == 100  # max(100, 80) = 100


def test_dedupe_url_encoding_preserved():
    """Query params should preserve their original encoding (no decoding).

    Tracking params are stripped by name, but the remaining query string
    preserves the original encoded form — %xx and + are NOT decoded.
    """
    from newsbot.dedupe import _canonical_url
    # Param value with spaces — original encoding preserved (not re-encoded)
    url = "https://example.com/search?q=hello world&sort=desc"
    canon = _canonical_url(url)
    # Original encoding preserved — no + or %20 added.
    assert "hello world" in canon
    assert "sort=desc" in canon

    # Percent-encoded values preserved (not decoded by parse_qsl).
    signed_url = "https://example.com/s?sig=abc%2Bdef&ts=12345"
    canon2 = _canonical_url(signed_url)
    assert "ts=12345" in canon2
    # %2B must stay as %2B (not decoded to + or space).
    assert "abc%2Bdef" in canon2, f"Percent encoding destroyed: {canon2}"


def test_dedupe_interleaved_same_source_engagement_order_independent():
    """Same-source engagement must use MAX (not sum) even when duplicates
    are interleaved with other-source merges.

    (A, B, A') and (A, A', B) should produce identical results.
    """
    from newsbot.collectors.base import new_candidate

    # (A, B, A') order: A=hn(100), B=reddit(50), A'=hn(80)
    a1 = new_candidate(title="Story", url="https://example.com/s",
                       source="hn", source_name="Hacker News", upvotes=100)
    b1 = new_candidate(title="Story", url="https://example.com/s",
                       source="reddit", source_name="Reddit", upvotes=50)
    a1b = new_candidate(title="Story", url="https://example.com/s",
                        source="hn", source_name="Hacker News", upvotes=80)
    out1 = dedupe_and_merge([a1, b1, a1b])

    # (A, A', B) order: A=hn(100), A'=hn(80), B=reddit(50)
    a2 = new_candidate(title="Story", url="https://example.com/s",
                       source="hn", source_name="Hacker News", upvotes=100)
    a2b = new_candidate(title="Story", url="https://example.com/s",
                        source="hn", source_name="Hacker News", upvotes=80)
    b2 = new_candidate(title="Story", url="https://example.com/s",
                       source="reddit", source_name="Reddit", upvotes=50)
    out2 = dedupe_and_merge([a2, a2b, b2])

    assert len(out1) == 1
    assert len(out2) == 1
    # Both should have same total upvotes: max(100,80) for hn + 50 for reddit = 150
    assert out1[0]["upvotes"] == 150, f"(A,B,A') got {out1[0]['upvotes']}"
    assert out2[0]["upvotes"] == 150, f"(A,A',B) got {out2[0]['upvotes']}"
    # Both should have same contributing sources
    assert set(out1[0]["contributing_sources"]) == {"hn", "reddit"}
    assert set(out2[0]["contributing_sources"]) == {"hn", "reddit"}
    # Both should have same crosspost_count
    assert out1[0]["crosspost_count"] == out2[0]["crosspost_count"]

    # Test with a HIGHER same-source value in the interleaved position.
    # (A, B, A'') where A''=hn(200) — should be max(100,200)=200 for hn, +50=250 total
    a3 = new_candidate(title="Story", url="https://example.com/s",
                       source="hn", source_name="Hacker News", upvotes=100)
    b3 = new_candidate(title="Story", url="https://example.com/s",
                       source="reddit", source_name="Reddit", upvotes=50)
    a3b = new_candidate(title="Story", url="https://example.com/s",
                        source="hn", source_name="Hacker News", upvotes=200)
    out3 = dedupe_and_merge([a3, b3, a3b])
    assert out3[0]["upvotes"] == 250, f"interleaved max got {out3[0]['upvotes']}"

    # Same result regardless of order
    a4 = new_candidate(title="Story", url="https://example.com/s",
                       source="hn", source_name="Hacker News", upvotes=100)
    a4b = new_candidate(title="Story", url="https://example.com/s",
                        source="hn", source_name="Hacker News", upvotes=200)
    b4 = new_candidate(title="Story", url="https://example.com/s",
                       source="reddit", source_name="Reddit", upvotes=50)
    out4 = dedupe_and_merge([a4, a4b, b4])
    assert out4[0]["upvotes"] == 250, f"grouped max got {out4[0]['upvotes']}"


def test_canonical_url_preserves_percent_encoding():
    """URLs with %xx encoding must preserve the encoded form, not decode it."""
    # %2F is encoded slash — must stay as %2F, not become /
    url = "https://example.com/path%2Fsegment?id=42"
    canon = _canonical_url(url)
    assert "%2F" in canon or "path%2Fsegment" in canon, f"Encoding destroyed: {canon}"
    assert "id=42" in canon


def test_canonical_url_preserves_plus_sign():
    """Plus signs in query values must be preserved, not decoded to spaces."""
    url = "https://example.com/search?q=hello+world&sort=desc"
    canon = _canonical_url(url)
    assert "hello+world" in canon, f"Plus sign decoded: {canon}"


def test_dedupe_transitive_merge_three_sources():
    """A+B merged, then C matches B's URL — should find the merged group, not split.

    Without index updates after merge, C would form a separate group.
    With index updates, C finds the merged group and crosspost_count == 3.
    """
    a = new_candidate(title="Story A", url="https://example.com/a",
                      source="hn", source_name="Hacker News", upvotes=100)
    b = new_candidate(title="Story A", url="https://example.com/b",
                      source="reddit", source_name="Reddit", upvotes=200)
    c = new_candidate(title="Story A", url="https://example.com/b",
                      source="github", source_name="GitHub", stars=500)
    out = dedupe_and_merge([a, b, c])
    # All three should be in one group (A matches B by title, C matches B by URL)
    assert len(out) == 1
    assert out[0]["crosspost_count"] == 3


def test_dedupe_transitive_url_match():
    """C matches B's URL only (not title) — should still find the merged group."""
    a = new_candidate(title="Breaking News", url="https://example.com/original",
                      source="hn", source_name="Hacker News", upvotes=100)
    b = new_candidate(title="Breaking News", url="https://example.com/different",
                      source="reddit", source_name="Reddit", upvotes=200)
    # C has same URL as B but different title — must match the merged group via URL index
    c = new_candidate(title="Different Title Entirely", url="https://example.com/different",
                      source="github", source_name="GitHub", stars=500)
    out = dedupe_and_merge([a, b, c])
    assert len(out) == 1, f"Transitive duplicate split: got {len(out)} groups"
    assert out[0]["crosspost_count"] == 3

def test_published_at_order_independent():
    """published_at must be the merged max regardless of collector order."""
    a = new_candidate(
        title="Same Story", url="https://example.com/post",
        source="hn", source_name="Hacker News", upvotes=100, comments=50,
        published_at="2026-07-20T00:00:00Z",
    )
    b = new_candidate(
        title="Same Story", url="https://example.com/post",
        source="reddit", source_name="r/news", upvotes=200, comments=80,
        published_at="2026-07-28T00:00:00Z",
    )
    # Forward order: [hn(old), reddit(new)]
    out_forward = dedupe_and_merge([a, b])
    # Reverse order: [reddit(new), hn(old)]
    out_reverse = dedupe_and_merge([b, a])
    assert len(out_forward) == 1
    assert len(out_reverse) == 1
    # Both must produce the same published_at (the max).
    assert out_forward[0]["published_at"] == out_reverse[0]["published_at"]
    # And the max should be the newer timestamp.
    assert out_forward[0]["published_at"] == "2026-07-28T00:00:00Z"


def test_published_at_preserved_when_primary_switches():
    """When primary source switches, published_at must stay as merged max."""
    # Reddit has higher engagement → becomes primary.
    # HN has the newer timestamp.
    a = new_candidate(
        title="Big Release", url="https://example.com/post",
        source="hn", source_name="Hacker News", upvotes=10, comments=5,
        published_at="2026-07-28T12:00:00Z",
    )
    b = new_candidate(
        title="Big Release", url="https://example.com/post",
        source="reddit", source_name="r/news", upvotes=5000, comments=300,
        published_at="2026-07-20T08:00:00Z",
    )
    out = dedupe_and_merge([a, b])
    assert len(out) == 1
    # Reddit should be primary (higher engagement).
    assert out[0]["source"] == "reddit"
    # But published_at should be the max (HN's newer timestamp).
    assert out[0]["published_at"] == "2026-07-28T12:00:00Z"


# --- match_candidate_to_store (flow_001094, store matching, Task 4) ------


def _store_row(row_id: int, title: str, url: str, *, merged_urls: str | None = None) -> dict:
    """Minimal store row shape as returned by db.list_store_rows()."""
    return {
        "id": row_id,
        "title": title,
        "url": url,
        "merge_count": 1,
        "merged_urls": merged_urls,
    }


def test_match_store_same_url_with_tracking_params():
    """Canonicalization strips utm_* / ref / fbclid etc — candidate with
    tracking params matches a row stored with the bare URL."""
    row = _store_row(1, "Tool X released", "https://example.com/post")
    candidate = new_candidate(
        title="Tool X released",
        url="https://www.example.com/post?utm_source=x&utm_medium=y&ref=hn",
        source="hn", source_name="HN",
    )
    assert match_candidate_to_store(candidate, [row]) is row


def test_match_store_github_repo_key_with_different_urls():
    """GitHub repo key matches when candidate URL AND title differ from the
    row — only the repo identity (full_name vs row URL repo) can match."""
    row = _store_row(1, "owner/repo", "https://github.com/owner/repo")
    candidate = new_candidate(
        title="Owner Repo (trending today)",
        url="https://api.github.com/repos/owner/repo",
        source="github", source_name="GitHub Trending",
        raw_json={"full_name": "Owner/Repo"},
    )
    assert match_candidate_to_store(candidate, [row]) is row


def test_match_store_fuzzy_title_above_threshold():
    """Normalized titles differ (exact check fails) but fuzzy ratio > 90."""
    row = _store_row(
        1, "Researchers unveil new method for scaling language model training",
        "https://a.com/story",
    )
    candidate = new_candidate(
        title="Researchers unveil a new method for scaling language model training",
        url="https://b.com/other", source="reddit", source_name="r/x",
    )
    # Sanity: ratio is above threshold but titles are not exactly equal.
    from newsbot.dedupe import _fuzzy_ratio
    ratio = _fuzzy_ratio(
        _normalize_title(candidate["title"]), _normalize_title(row["title"])
    )
    assert _normalize_title(candidate["title"]) != _normalize_title(row["title"])
    assert ratio > FUZZY_THRESHOLD
    assert match_candidate_to_store(candidate, [row]) is row


def test_match_store_no_false_positive_below_threshold():
    """Distinct stories: URLs and titles differ, fuzzy ratio well below 90."""
    row = _store_row(
        1, "New GPU benchmark released for data centers", "https://a.com/gpu",
    )
    candidate = new_candidate(
        title="New CPU cooler reviewed for quiet builds",
        url="https://b.com/cpu", source="hn", source_name="HN",
    )
    assert match_candidate_to_store(candidate, [row]) is None


def test_match_store_merged_urls_entry():
    """Candidate matches a URL stored only in the merged_urls JSON string,
    not in row['url'] — with a title that fails all other checks."""
    row = _store_row(
        1, "Breaking News", "https://example.com/original",
        merged_urls=json.dumps(["https://example.com/alternate"]),
    )
    candidate = new_candidate(
        title="Completely Different Title",
        url="https://example.com/alternate?utm_source=x",
        source="reddit", source_name="Reddit",
    )
    assert match_candidate_to_store(candidate, [row]) is row


def test_match_store_no_match_returns_none():
    rows = [
        _store_row(1, "Story A", "https://a.com"),
        _store_row(2, "Story B", "https://b.com"),
    ]
    candidate = new_candidate(
        title="Something else entirely", url="https://c.com/other",
        source="hn", source_name="HN",
    )
    assert match_candidate_to_store(candidate, rows) is None
    # Empty store also yields None.
    assert match_candidate_to_store(candidate, []) is None


def test_match_store_malformed_merged_urls_falls_through():
    """Bad merged_urls JSON must not crash; other identity checks still run."""
    candidate_title = new_candidate(
        title="Broken JSON Row Story", url="https://z.com/none",
        source="hn", source_name="HN",
    )
    # Exact-title match still works despite malformed merged_urls.
    row = _store_row(1, "Broken JSON Row Story", "https://a.com/story",
                     merged_urls="{not valid json")
    assert match_candidate_to_store(candidate_title, [row]) is row
    # No identity match at all with malformed merged_urls -> None, no exception.
    candidate_none = new_candidate(
        title="Unrelated story", url="https://z.com/other",
        source="hn", source_name="HN",
    )
    assert match_candidate_to_store(candidate_none, [row]) is None
    # JSON that parses but is not a list of strings is tolerated too.
    row2 = _store_row(2, "Some Story", "https://a.com/s",
                      merged_urls=json.dumps({"not": "a list"}))
    candidate_other = new_candidate(
        title="Some Story", url="https://z.com/x", source="hn", source_name="HN",
    )
    assert match_candidate_to_store(candidate_other, [row2]) is row2


def test_match_store_exact_title_match():
    """Check 3: normalized-title exact match with distinct URLs."""
    row = _store_row(1, "Exact Title Story", "https://a.com/x")
    candidate = new_candidate(
        title="  Exact   Title Story ", url="https://b.com/y",
        source="reddit", source_name="Reddit",
    )
    assert match_candidate_to_store(candidate, [row]) is row


def test_match_store_first_match_wins_by_check_order():
    """Mirrors dedupe_and_merge: checks run at check level across all rows,
    not row-by-row. Row 1 matches on exact title, but row 2 owns the
    candidate's URL — the URL check (2) runs before the title check (3),
    so row 2 wins."""
    row1 = _store_row(1, "Unrelated title for row one", "https://a.com/x")
    row2 = _store_row(2, "Unrelated title for row two", "https://b.com/y")
    candidate = new_candidate(
        title="Unrelated title for row one", url="https://b.com/y",
        source="hn", source_name="HN",
    )
    assert match_candidate_to_store(candidate, [row1, row2]) is row2


# --- Trends containment dedupe rule (H-3) --------------------------------

from newsbot.dedupe import _trend_tokens, _trends_containment_match


def test_trend_tokens_strips_stopwords():
    """Stopwords are removed; remaining tokens returned."""
    tokens = _trend_tokens("GTA 6 leak")
    assert "gta" in tokens
    assert "6" in tokens
    assert "leak" in tokens
    assert len(tokens) == 3


def test_trend_tokens_drops_stopwords():
    """High-frequency tokens are dropped."""
    tokens = _trend_tokens("the best new game")
    # "the", "best", "new" are stopwords
    assert "game" in tokens
    assert "the" not in tokens
    assert "best" not in tokens
    assert "new" not in tokens


def test_trends_containment_matches_all_tokens():
    """Trend 'GTA 6 leak' matches an article containing all trend tokens
    as whole words."""
    trends_item = new_candidate(
        title="GTA 6 leak confirmed ahead of release",
        url="https://ign.com/gta6",
        source="trends",
        source_name="trends/GTA 6 leak",
    )
    article_item = new_candidate(
        title="GTA 6 leak confirmed: gameplay footage online ahead of release",
        url="https://ign.com/gta6-article",
        source="rss",
        source_name="IGN",
    )
    assert _trends_containment_match(trends_item, article_item) is True


def test_trends_containment_does_not_match_partial():
    """Trend 'GTA 6 leak' does NOT match 'Leak in GTA 5 RP server' —
    not ALL trend tokens present (6 is missing, leak alone is <2 tokens after
    normalization but the key is 'gta', '6', 'leak' and 5≠6)."""
    trends_item = new_candidate(
        title="Leak in GTA 5 RP server",
        url="https://reddit.com/gta5rp",
        source="trends",
        source_name="trends/GTA 6 leak",
    )
    article_item = new_candidate(
        title="Leak in GTA 5 RP server",
        url="https://reddit.com/gta5rp",
        source="reddit",
        source_name="r/gaming",
    )
    # "gta" is in the title, but "6" is not (it has "5"), and "leak" is there.
    # Not ALL tokens present → no match.
    assert _trends_containment_match(trends_item, article_item) is False


def test_trends_containment_rejects_substring_digit_in_year():
    """Regression: substring matching made "6" match inside "2026".
    Trend 'GTA 6 leak' must NOT merge with an article about 2026 that
    mentions gta/leak but not the standalone token '6'."""
    trends_item = new_candidate(
        title="placeholder",
        url="https://trends.google.com/x",
        source="trends",
        source_name="trends/GTA 6 leak",
    )
    article_item = new_candidate(
        title="GTA leak: biggest 2026 release news",
        url="https://kotaku.com/gta-2026",
        source="rss",
        source_name="Kotaku",
    )
    # Old substring code: "gta" in title ✓, "6" in "2026" ✓, "leak" in title ✓
    # → false merge. Token-set containment requires whole token "6".
    assert _trends_containment_match(trends_item, article_item) is False


def test_trends_containment_rejects_substring_word_inside_word():
    """Regression: substring matching made "ai" match inside "maintain".
    Trend 'AI agents' must NOT merge with an article about maintaining
    agents where 'ai' only appears embedded in another word."""
    trends_item = new_candidate(
        title="placeholder",
        url="https://trends.google.com/y",
        source="trends",
        source_name="trends/AI agents",
    )
    article_item = new_candidate(
        title="How to maintain agents in production",
        url="https://blog.example.com/maintain-agents",
        source="rss",
        source_name="Tech Blog",
    )
    # Old substring code: "ai" in "maintain" ✓, "agents" in title ✓
    # → false merge. Token-set containment requires whole token "ai".
    assert _trends_containment_match(trends_item, article_item) is False


def test_trends_containment_matches_with_extra_words():
    """Token-set containment still matches when the other title has
    additional words around the trend tokens."""
    trends_item = new_candidate(
        title="placeholder",
        url="https://trends.google.com/z",
        source="trends",
        source_name="trends/GTA 6 leak",
    )
    article_item = new_candidate(
        title="Massive GTA 6 leak: everything we know about the 2026 launch",
        url="https://ign.com/gta6-everything",
        source="rss",
        source_name="IGN",
    )
    # All of gta/6/leak appear as whole tokens → match.
    assert _trends_containment_match(trends_item, article_item) is True


def test_trends_containment_requires_min_2_tokens():
    """A single-token trend title (<2 after stopword removal) never matches."""
    trends_item = new_candidate(
        title="Some article",
        url="https://x.com",
        source="trends",
        source_name="trends/AI",  # 1 token
    )
    article_item = new_candidate(
        title="AI breakthrough",
        url="https://y.com",
        source="hn",
        source_name="Hacker News",
    )
    assert _trends_containment_match(trends_item, article_item) is False


def test_trends_containment_not_scoped_to_non_trends():
    """A non-trends candidate never triggers the containment rule."""
    item = new_candidate(
        title="GTA 6 leak",
        url="https://x.com",
        source="rss",
        source_name="trends/GTA 6 leak",  # has trends/ prefix but source != trends
    )
    other = new_candidate(
        title="GTA 6 leak confirmed",
        url="https://y.com",
        source="hn",
        source_name="HN",
    )
    assert _trends_containment_match(item, other) is False


def test_trends_containment_matches_inflected_token():
    """Headlines inflect: trend 'GTA 6 leak' must match 'GTA 6 gameplay
    leaks online…' (the plan's acceptance example). Prefix matching only
    applies to tokens of 4+ chars, so '6' and 'ai' stay exact."""
    trends_item = new_candidate(
        title="GTA 6 leak",
        url="https://trends.google.com/abc",
        source="trends",
        source_name="trends/GTA 6 leak",
    )
    assert _trends_containment_match(trends_item, new_candidate(
        title="GTA 6 gameplay leaks online ahead of launch",
        url="https://ign.com/gta6-leaks",
        source="rss",
        source_name="IGN",
    )) is True
    assert _trends_containment_match(trends_item, new_candidate(
        title="GTA 6 footage leaked, Rockstar responds",
        url="https://ign.com/gta6-leaked",
        source="rss",
        source_name="IGN",
    )) is True
    # Short tokens never prefix-match: '6' must not match '60fps'.
    assert _trends_containment_match(trends_item, new_candidate(
        title="GTA 60fps leak patch",
        url="https://ign.com/gta-60fps",
        source="rss",
        source_name="IGN",
    )) is False


def test_reddit_link_post_merges_with_linked_article():
    """A Reddit link post carries the article in raw_json.external_url;
    it must merge with the RSS item for that article even when the
    titles differ."""
    article = new_candidate(
        title="GTA 6 gameplay footage leaks online ahead of launch",
        url="https://www.ign.com/articles/gta-6-leak?utm_source=rss",
        source="rss",
        source_name="IGN",
    )
    reddit_post = new_candidate(
        title="Holy cow, GTA 6 just leaked",
        url="https://www.reddit.com/r/gaming/comments/abc/holy_cow/",
        source="reddit",
        source_name="r/gaming",
        upvotes=22000,
        comments=1100,
        raw_json={"external_url": "https://www.ign.com/articles/gta-6-leak"},
    )
    out = dedupe_and_merge([article, reddit_post])
    assert len(out) == 1
    assert out[0]["crosspost_count"] == 2
    assert out[0]["upvotes"] == 22000

    # Self-posts point at themselves — no spurious merges, no crash.
    self_post = new_candidate(
        title="Weekly discussion thread",
        url="https://www.reddit.com/r/gaming/comments/def/weekly/",
        source="reddit",
        source_name="r/gaming",
        raw_json={"external_url": "https://www.reddit.com/r/gaming/comments/def/weekly/"},
    )
    assert len(dedupe_and_merge([article, self_post])) == 2


def test_trends_containment_dedupe_merges():
    """dedupe_and_merge merges a trends candidate with a matching article."""
    trends = new_candidate(
        title="GTA 6 gameplay leaks online ahead of release",
        url="https://trends.google.com/abc",
        source="trends",
        source_name="trends/GTA 6 leak",
        reposts=1000,
    )
    article = new_candidate(
        title="GTA 6 gameplay leaks online ahead of release",
        url="https://ign.com/articles/gta6",
        source="rss",
        source_name="IGN",
        upvotes=50,
    )
    # Put the article first, then the trends candidate.
    out = dedupe_and_merge([article, trends])
    assert len(out) == 1
    merged = out[0]
    # crosspost = 2 (trends + rss), reposts carried in, upvotes summed.
    assert merged["crosspost_count"] == 2
    assert merged["reposts"] == 1000
    assert merged["upvotes"] == 50


def test_trends_containment_dedupe_does_not_merge_partial():
    """dedupe_and_merge does NOT merge trends 'GTA 6 leak' with 'Leak in GTA 5 RP'."""
    trends = new_candidate(
        title="Leak in GTA 5 RP server",
        url="https://trends.google.com/abc",
        source="trends",
        source_name="trends/GTA 6 leak",
        reposts=200,
    )
    article = new_candidate(
        title="Leak in GTA 5 RP server",
        url="https://reddit.com/r/gaming/comments/x/gta5rp",
        source="reddit",
        source_name="r/gaming",
        upvotes=100,
    )
    # URL and title match exactly → they WILL merge (title match, not trends containment).
    # But the trends containment rule itself should not fire.
    # Let's test with different URLs so only the trends rule is the candidate path:
    trends2 = new_candidate(
        title="Leak in GTA 5 RP server",
        url="https://trends.google.com/def",
        source="trends",
        source_name="trends/GTA 6 leak",
        reposts=200,
    )
    article2 = new_candidate(
        title="GTA 5 RP server has a leak",
        url="https://reddit.com/r/gaming/comments/y/gta5rp2",
        source="reddit",
        source_name="r/gaming",
        upvotes=100,
    )
    out = dedupe_and_merge([article2, trends2])
    # trends2's title is "Leak in GTA 5 RP server" which is different from
    # article2's title "GTA 5 RP server has a leak" — no exact/fuzzy match.
    # The trends containment check: tokens from "GTA 6 leak" are [gta, 6, leak].
    # "gta 5 rp server has a leak" contains "gta" and "leak" but NOT "6".
    # So no match → 2 separate items.
    assert len(out) == 2
