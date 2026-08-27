"""Tests for flow_001125: Primary-URL preference for reddit link posts.

Covers:
  AC 1: reddit link post (is_self=False, external_url valid http(s)) inserted
        -> store row url is the article URL; permalink in merged_urls AND in
        seen; next-cycle re-collection of the same permalink merges/drops.
  AC 2: reddit self post -> url stays the permalink, behaviour unchanged.
  AC 3: external_url missing, non-http, or pointing at reddit.com/redd.it
        -> no swap.
  AC 4: merged candidate whose primary is a link post -> swap still applies
        and no contributing URL is lost.
  AC 5: scoring/dedupe unit tests untouched and passing (swap happens after
        classification). Full suite green (asserted by test count, not here).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from newsbot.collectors.base import new_candidate
from newsbot.db import NewsStore
from newsbot.dedupe import (
    _canonical_url,
    dedupe_and_merge,
    match_candidate_to_store,
)
from newsbot.main import _swap_reddit_link_post_url


# --- helpers ---------------------------------------------------------------


def _iso(days_ago: float = 0.0) -> str:
    """ISO timestamp `days_ago` days before now."""
    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).isoformat(timespec="seconds")


def _bd(**overrides) -> dict:
    base = {
        "score": 100.0,
        "engagement": 80.0,
        "recency": 0.9,
        "source_weight": 1.0,
        "topic_bonus": 0,
        "crosspost_bonus": 0.0,
        "penalty": 1.0,
        "matched_topics": [],
        "origin_topic": "gaming",
        "scored_at": _iso(0),
        "lookback_hours": 48.0,
        "source": "reddit",
        "published_at": _iso(0),
        "upvotes": 100,
        "comments": 10,
        "stars": 0,
        "reposts": 0,
        "crosspost_count": 1,
    }
    base.update(overrides)
    return base


def _reddit_link_story(
    title: str = "Xbox Benchmark",
    *,
    permalink: str = "https://www.reddit.com/r/gaming/comments/abc/xbox_benchmark/",
    external_url: str = "https://news.xbox.com/2025/06/benchmark",
    is_self: bool = False,
    contributing_urls: list[str] | None = None,
    **bd_overrides,
) -> dict:
    """Build a reddit link-post story dict suitable for add_stories_to_store."""
    return {
        "title": title,
        "url": permalink,
        "source": "reddit",
        "source_name": "r/gaming",
        "snippet": f"Snippet for {title}",
        "raw_json": {
            "external_url": external_url,
            "is_self": is_self,
            "permalink": permalink.replace("https://www.reddit.com", ""),
            "subreddit": "gaming",
        },
        "contributing_urls": contributing_urls or [],
        "score_breakdown": _bd(**bd_overrides),
    }


def _reddit_self_story(
    title: str = "Self Post Discussion",
    *,
    permalink: str = "https://www.reddit.com/r/gaming/comments/xyz/self_post/",
    **bd_overrides,
) -> dict:
    """Build a reddit self-post story dict."""
    return {
        "title": title,
        "url": permalink,
        "source": "reddit",
        "source_name": "r/gaming",
        "snippet": f"Snippet for {title}",
        "raw_json": {
            "external_url": permalink,
            "is_self": True,
            "permalink": permalink.replace("https://www.reddit.com", ""),
            "subreddit": "gaming",
        },
        "contributing_urls": [],
        "score_breakdown": _bd(**bd_overrides),
    }


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    s = NewsStore(tmp_path / "reddit_url_swap.sqlite")
    yield s
    s.close()


# --- AC 1: Reddit link post URL swap ---------------------------------------


class TestRedditLinkPostSwap:
    """AC 1: reddit link post inserted -> row url is article URL; permalink
    in merged_urls AND seen; re-collected permalink merges/drops."""

    def test_swap_changes_url_to_external(self):
        """The swap function replaces url with external_url and archives
        the permalink into contributing_urls."""
        article = "https://news.xbox.com/2025/06/benchmark"
        permalink = "https://www.reddit.com/r/gaming/comments/abc/xbox_benchmark/"
        item = _reddit_link_story(permalink=permalink, external_url=article)

        _swap_reddit_link_post_url(item)

        assert item["url"] == article
        assert permalink in item["contributing_urls"]

    def test_inserted_row_url_is_article(self, store):
        """After swap + add_stories_to_store, the DB row's url is the article
        URL, not the reddit permalink."""
        article = "https://news.xbox.com/2025/06/benchmark"
        permalink = "https://www.reddit.com/r/gaming/comments/abc/xbox_benchmark/"
        item = _reddit_link_story(permalink=permalink, external_url=article)

        _swap_reddit_link_post_url(item)
        store.add_stories_to_store([item], [])

        row = store._conn.execute(
            "SELECT url, merged_urls FROM pending_posts"
        ).fetchone()
        assert row["url"] == article
        merged = json.loads(row["merged_urls"] or "[]")
        assert permalink in merged, "permalink must be in merged_urls"

    def test_permalink_in_seen_after_insert(self, store):
        """After swap + add_stories_to_store, the permalink is in the seen
        table (via contributing_urls seeding into seen_items)."""
        article = "https://news.xbox.com/2025/06/benchmark"
        permalink = "https://www.reddit.com/r/gaming/comments/abc/xbox_benchmark/"
        item = _reddit_link_story(permalink=permalink, external_url=article)

        _swap_reddit_link_post_url(item)

        # Build seen_items the same way _run_generation does: item itself
        # + all contributing_urls as {url, title} dicts.
        seen_items = [item]
        for curl in (item.get("contributing_urls") or []):
            cs = str(curl or "").strip()
            if cs:
                seen_items.append({"url": cs, "title": item["title"]})

        store.add_stories_to_store([item], seen_items)

        assert store.is_seen(permalink, item["title"]), \
            "permalink must be in seen table after insert"

    def test_recollected_permalink_no_new_row(self, store):
        """Full cycle: swap → insert → seen-mark. Next cycle, re-collecting
        the same permalink is caught by is_seen (filter_seen drops it)
        — no new row."""
        article = "https://news.xbox.com/2025/06/benchmark"
        permalink = "https://www.reddit.com/r/gaming/comments/abc/xbox_benchmark/"
        item = _reddit_link_story(permalink=permalink, external_url=article)

        _swap_reddit_link_post_url(item)
        seen_items = [item]
        for curl in (item.get("contributing_urls") or []):
            seen_items.append({"url": curl, "title": item["title"]})
        store.add_stories_to_store([item], seen_items)

        # Simulate next-cycle filter_seen: the permalink is in seen.
        assert store.is_seen(permalink, item["title"])

        # And match_candidate_to_store: a candidate carrying the article URL
        # would match the existing row (via url equality).
        rows = store.list_store_rows()
        candidate = new_candidate(
            title="Xbox Benchmark",
            url=article,
            source="rss",
            source_name="IGN",
        )
        hit = match_candidate_to_store(candidate.to_dict(), rows)
        assert hit is not None, \
            "re-collected article URL must match the existing row"


# --- AC 2: Reddit self post keeps permalink --------------------------------


class TestRedditSelfPostNoSwap:
    """AC 2: reddit self post -> url stays the permalink, no swap."""

    def test_self_post_url_unchanged(self):
        """Self post (is_self=True) — swap is a no-op, url stays permalink."""
        permalink = "https://www.reddit.com/r/gaming/comments/xyz/self_post/"
        item = _reddit_self_story(permalink=permalink)

        _swap_reddit_link_post_url(item)

        assert item["url"] == permalink
        assert item["contributing_urls"] == []

    def test_self_post_inserted_url_is_permalink(self, store):
        """Self post inserted into store — row url is the permalink."""
        permalink = "https://www.reddit.com/r/gaming/comments/xyz/self_post/"
        item = _reddit_self_story(permalink=permalink)

        _swap_reddit_link_post_url(item)
        store.add_stories_to_store([item], [])

        row = store._conn.execute(
            "SELECT url FROM pending_posts"
        ).fetchone()
        assert row["url"] == permalink


# --- AC 3: Skip conditions -------------------------------------------------


class TestSwapSkipConditions:
    """AC 3: external_url missing, non-http, or pointing at
    reddit.com/redd.it -> no swap."""

    def test_missing_external_url_no_swap(self):
        """raw_json has no external_url -> no swap."""
        item = _reddit_link_story()
        item["raw_json"].pop("external_url")
        original_url = item["url"]

        _swap_reddit_link_post_url(item)

        assert item["url"] == original_url

    def test_none_external_url_no_swap(self):
        """external_url is None -> no swap."""
        item = _reddit_link_story()
        item["raw_json"]["external_url"] = None
        original_url = item["url"]

        _swap_reddit_link_post_url(item)

        assert item["url"] == original_url

    def test_non_http_external_url_no_swap(self):
        """external_url is not http(s) -> no swap."""
        item = _reddit_link_story()
        item["raw_json"]["external_url"] = "ftp://files.example.com/data"
        original_url = item["url"]

        _swap_reddit_link_post_url(item)

        assert item["url"] == original_url

    def test_reddit_host_external_url_no_swap(self):
        """external_url points at reddit.com -> no swap (crosspost)."""
        item = _reddit_link_story()
        item["raw_json"]["external_url"] = \
            "https://www.reddit.com/r/other/comments/def/crosspost/"
        original_url = item["url"]

        _swap_reddit_link_post_url(item)

        assert item["url"] == original_url

    def test_reddit_subdomain_external_url_no_swap(self):
        """external_url points at a reddit subdomain -> no swap."""
        item = _reddit_link_story()
        item["raw_json"]["external_url"] = \
            "https://oauth.reddit.com/r/other/comments/def/crosspost/"
        original_url = item["url"]

        _swap_reddit_link_post_url(item)

        assert item["url"] == original_url

    def test_redd_it_external_url_no_swap(self):
        """external_url points at redd.it -> no swap."""
        item = _reddit_link_story()
        item["raw_json"]["external_url"] = "https://redd.it/abc123"
        original_url = item["url"]

        _swap_reddit_link_post_url(item)

        assert item["url"] == original_url

    def test_non_reddit_source_no_swap(self):
        """source != reddit -> no swap."""
        item = _reddit_link_story()
        item["source"] = "rss"
        item["score_breakdown"]["source"] = "rss"
        original_url = item["url"]

        _swap_reddit_link_post_url(item)

        assert item["url"] == original_url

    def test_empty_external_url_no_swap(self):
        """external_url is empty string -> no swap."""
        item = _reddit_link_story()
        item["raw_json"]["external_url"] = ""
        original_url = item["url"]

        _swap_reddit_link_post_url(item)

        assert item["url"] == original_url

    def test_no_raw_json_no_swap(self):
        """raw_json is None -> no swap."""
        item = _reddit_link_story()
        item["raw_json"] = None
        original_url = item["url"]

        _swap_reddit_link_post_url(item)

        assert item["url"] == original_url

    def test_raw_json_not_dict_no_swap(self):
        """raw_json is a string (malformed) -> no swap."""
        item = _reddit_link_story()
        item["raw_json"] = "not a dict"
        original_url = item["url"]

        _swap_reddit_link_post_url(item)

        assert item["url"] == original_url

    def test_url_equals_external_no_swap(self):
        """url already equals external_url -> no swap (nothing to do)."""
        article = "https://news.xbox.com/2025/06/benchmark"
        item = _reddit_link_story(permalink=article, external_url=article)

        _swap_reddit_link_post_url(item)

        assert item["url"] == article
        assert item["contributing_urls"] == []


# --- AC 4: Merged candidate primary is a link post -------------------------


class TestMergedCandidateSwap:
    """AC 4: merged candidate whose primary is a link post -> swap still
    applies and no contributing URL is lost."""

    def test_swap_preserves_existing_contributing_urls(self):
        """A merged candidate already has contributing_urls from in-cycle
        dedupe. The swap must add the permalink to contributing_urls
        WITHOUT losing any existing entries."""
        article = "https://www.ign.com/articles/big-game-review"
        permalink = "https://www.reddit.com/r/gaming/comments/abc/big_game/"
        hn_url = "https://hn.algolia.com/story/big-game"
        existing_contributing = [
            hn_url,
            _canonical_url(article),
        ]
        item = _reddit_link_story(
            permalink=permalink,
            external_url=article,
            contributing_urls=existing_contributing,
        )

        _swap_reddit_link_post_url(item)

        # URL swapped
        assert item["url"] == article
        # Permalink archived
        assert permalink in item["contributing_urls"]
        # No existing contributing URL lost
        assert hn_url in item["contributing_urls"]
        assert _canonical_url(article) in item["contributing_urls"]
        # No duplicates
        assert len(item["contributing_urls"]) == len(set(item["contributing_urls"]))

    def test_swap_on_dedupe_merged_result(self, store):
        """Full flow: dedupe_and_merge produces a merged candidate whose
        primary is a reddit link post. After swap + insert, merged_urls
        contains ALL contributing URLs plus the permalink."""
        article = "https://www.ign.com/articles/big-game-review"
        permalink = "https://www.reddit.com/r/gaming/comments/abc/big_game/"
        hn_url = "https://hn.algolia.com/story/big-game"

        a = new_candidate(
            title="Big Game Review",
            url=hn_url,
            source="hn",
            source_name="Hacker News",
            upvotes=500,
        )
        b = new_candidate(
            title="Big Game Review",
            url=permalink,
            source="reddit",
            source_name="r/gaming",
            upvotes=2000,
            raw_json={
                "external_url": article,
                "is_self": False,
                "permalink": permalink.replace("https://www.reddit.com", ""),
                "subreddit": "gaming",
            },
        )

        merged = dedupe_and_merge([a.to_dict(), b.to_dict()])
        assert len(merged) == 1
        # The reddit permalink won the primary (higher engagement).
        assert merged[0]["url"] == permalink
        assert "contributing_urls" in merged[0]

        # Simulate _run_generation: classify as "add", then swap.
        item = {**merged[0], "action": "add", "merge_row_id": None}
        _swap_reddit_link_post_url(item)

        # URL is now the article.
        assert item["url"] == article

        # The permalink is in contributing_urls.
        assert permalink in item["contributing_urls"]

        # The HN URL is still there (not lost).
        assert hn_url in item["contributing_urls"]

        # Insert into store — merged_urls is seeded from contributing_urls
        # minus the row's own url (the article).
        story = {
            "title": item["title"],
            "url": item["url"],
            "source": item["source"],
            "source_name": item["source_name"],
            "snippet": f"Snippet for {item['title']}",
            "raw_json": item["raw_json"],
            "contributing_urls": item["contributing_urls"],
            "score_breakdown": _bd(),
        }
        store.add_stories_to_store([story], [])

        row = store._conn.execute(
            "SELECT url, merged_urls FROM pending_posts"
        ).fetchone()
        assert row["url"] == article
        merged_urls = json.loads(row["merged_urls"] or "[]")

        # The permalink is in merged_urls.
        assert permalink in merged_urls
        # The HN URL is in merged_urls.
        assert hn_url in merged_urls
        # The article URL (row's own url) is NOT in merged_urls.
        assert article not in merged_urls


# --- AC 5: Swap happens after classification -------------------------------


class TestSwapAfterClassification:
    """AC 5: the swap does not affect scoring/dedupe — it runs at the write
    boundary, after classification."""

    def test_swap_does_not_mutate_score_breakdown(self):
        """The swap changes url and contributing_urls only — score_breakdown
        is untouched."""
        article = "https://news.xbox.com/2025/06/benchmark"
        permalink = "https://www.reddit.com/r/gaming/comments/abc/xbox_benchmark/"
        item = _reddit_link_story(permalink=permalink, external_url=article)
        original_bd = json.dumps(item["score_breakdown"], sort_keys=True)

        _swap_reddit_link_post_url(item)

        assert json.dumps(item["score_breakdown"], sort_keys=True) == original_bd

    def test_swap_preserves_raw_json(self):
        """The swap does not modify raw_json."""
        article = "https://news.xbox.com/2025/06/benchmark"
        permalink = "https://www.reddit.com/r/gaming/comments/abc/xbox_benchmark/"
        item = _reddit_link_story(permalink=permalink, external_url=article)
        original_rj = json.dumps(item["raw_json"], sort_keys=True)

        _swap_reddit_link_post_url(item)

        assert json.dumps(item["raw_json"], sort_keys=True) == original_rj

    def test_match_candidate_to_store_uses_permalink_pre_swap(self, store):
        """Before the swap (classification time), match_candidate_to_store
        operates on the permalink, not the article URL. A candidate
        carrying the article URL matches the store row via external_url
        identity (flow_001123 machinery), proving classification ran on
        original fields."""
        article = "https://www.apple.com/newsroom/2025/mac-studio"
        permalink = "https://www.reddit.com/r/apple/comments/xyz/mac_studio/"

        # Store row: the RSS article (url = article_url).
        rss_row = {
            "title": "Apple announces Mac Studio",
            "url": article,
            "source": "rss",
            "source_name": "9to5Mac",
            "snippet": "Snippet",
            "score_breakdown": _bd(source="rss"),
        }
        store.add_stories_to_store([rss_row], [])

        # Reddit link post candidate (pre-swap: url=permalink).
        candidate = new_candidate(
            title="Mac Studio M4 is here!",
            url=permalink,
            source="reddit",
            source_name="r/apple",
            raw_json={"external_url": article, "is_self": False},
        )

        rows = store.list_store_rows()
        hit = match_candidate_to_store(candidate.to_dict(), rows)
        assert hit is not None, \
            "classification must match via external_url identity (pre-swap)"
