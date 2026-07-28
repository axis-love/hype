"""Tests for newsbot/dedupe.py — canonical URL, title fuzzy, GitHub repo, merge."""

from newsbot.collectors.base import new_candidate
from newsbot.dedupe import (
    _canonical_url,
    _normalize_title,
    dedupe_and_merge,
    _merge_pair,
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