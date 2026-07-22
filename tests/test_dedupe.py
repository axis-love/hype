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
    keep = new_candidate(title="t", url="u", source="hn", source_name="HN", snippet="short")
    other = new_candidate(title="t", url="u", source="reddit", source_name="r/x", snippet="a much longer snippet than the first one")
    _merge_pair(keep, other)
    assert "much longer snippet" in keep["snippet"]