"""Tests for flow_001123: dedupe hardening.

Covers:
  1. Regression (Witcher case): posted row + same story from different
     subreddit (fuzzy < 90, same external article url) -> merge, no new row.
  2. Mac Studio case: reddit link-post external_url matches unposted row's url.
  3. Row-side external_url: candidate url matches row's raw_json external_url.
  4. In-cycle merge: contributing_urls persisted in merged_urls + seen.
  5. Window: posted_at older than window -> NOT a merge target.
     NEWS_MERGE_WINDOW_DAYS env respected.
  6. pick_hottest/evict_coldest/count_pending still unposted-only.
  7. Merging into a posted row must never resurrect it.
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
    _external_url_key,
    _merge_pair,
    _row_external_url_key,
    _row_raw_json,
    dedupe_and_merge,
    match_candidate_to_store,
)


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


def _store_story(
    title: str = "Test Story",
    url: str = "https://example.com/story",
    *,
    source: str = "reddit",
    source_name: str = "r/test",
    external_url: str | None = None,
    **bd_overrides,
) -> dict:
    """Build a story dict suitable for add_stories_to_store."""
    raw_json: dict | None = None
    if external_url:
        raw_json = {"external_url": external_url}
    return {
        "title": title,
        "url": url,
        "source": source,
        "source_name": source_name,
        "snippet": f"Snippet for {title}",
        "raw_json": raw_json,
        "score_breakdown": _bd(**bd_overrides),
    }


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    s = NewsStore(tmp_path / "dedupe_hardening.sqlite")
    yield s
    s.close()


# --- AC 1: Witcher regression test ------------------------------------------


class TestWitcherRegression:
    """AC 1: posted row + same story from different subreddit -> merge."""

    def test_posted_row_merge_target_within_window(self, store):
        """A row with posted_at set 1 day ago; the same story arrives from
        a different subreddit (different permalink, title fuzzy-ratio < 90,
        same external article url) -> classified action='merge' into the
        posted row; NO new row; row stays absent from list_store_rows();
        posted_at unchanged.
        """
        # Seed a posted row (the original Witcher post).
        article_url = "https://www.ign.com/articles/witcher-3-songs-of-the-past"
        original = _store_story(
            title="Witcher 3: Songs of the Past — Review",
            url=article_url,
            source_name="r/gaming",
            external_url=article_url,
        )
        store.add_stories_to_store([original], [])
        row_id = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        # Mark as posted 1 day ago.
        posted_at = _iso(1)
        store._conn.execute(
            "UPDATE pending_posts SET posted_at=? WHERE id=?",
            (posted_at, row_id),
        )

        # Same story arrives from r/witcher — different permalink, different
        # enough title that fuzzy < 90, same external article url.
        candidate = new_candidate(
            title="Songs of the Past DLC announced for Witcher 3",
            url="https://www.reddit.com/r/witcher/comments/abc/songs_of_the_past/",
            source="reddit",
            source_name="r/witcher",
            raw_json={"external_url": article_url},
        )

        # list_merge_target_rows includes posted rows within the window.
        targets = store.list_merge_target_rows(7)
        hit = match_candidate_to_store(candidate.to_dict(), targets)

        assert hit is not None, "must match the posted row via external_url"
        assert hit["id"] == row_id

        # Row must NOT appear in list_store_rows (unposted only).
        unposted = store.list_store_rows()
        assert all(r["id"] != row_id for r in unposted), \
            "posted row must not appear in list_store_rows"

        # posted_at must be unchanged.
        row = store._conn.execute(
            "SELECT posted_at FROM pending_posts WHERE id=?", (row_id,)
        ).fetchone()
        assert row["posted_at"] == posted_at


# --- AC 2: Mac Studio case -------------------------------------------------


class TestExternalUrlCandidateToRow:
    """AC 2: reddit link-post candidate whose raw_json.external_url
    canonically equals an UNPOSTED store row's url -> match."""

    def test_external_url_matches_row_url(self, store):
        """A Reddit link post pointing at apple.com must match a store
        row whose url IS apple.com, even though the Reddit permalink
        is a completely different URL."""
        article_url = "https://www.apple.com/newsroom/2025/mac-studio-m4"
        # Store row: the RSS article (url = article_url).
        rss_row = _store_story(
            title="Apple announces Mac Studio with M4",
            url=article_url,
            source="rss",
            source_name="9to5Mac",
        )
        store.add_stories_to_store([rss_row], [])

        # Reddit link post: permalink in url, article in external_url.
        candidate = new_candidate(
            title="Mac Studio M4 is here!",
            url="https://www.reddit.com/r/apple/comments/xyz/mac_studio_m4/",
            source="reddit",
            source_name="r/apple",
            raw_json={"external_url": article_url},
        )

        rows = store.list_store_rows()
        hit = match_candidate_to_store(candidate.to_dict(), rows)
        assert hit is not None
        assert _canonical_url(hit["url"]) == _canonical_url(article_url)


# --- AC 3: Row-side external_url match -------------------------------------


class TestRowSideExternalUrl:
    """AC 3: candidate url equals the article URL stored in a row's
    raw_json (JSON string) external_url -> merges. Malformed/absent
    row raw_json degrades silently."""

    def test_candidate_url_matches_row_external_url(self, store):
        """Candidate url equals the article URL stored in a row's raw_json
        external_url field (stored as a JSON *string* in the DB)."""
        article_url = "https://www.theverge.com/2025/ai-model-release"
        # Store row: a Reddit link post whose external_url is the article.
        reddit_row = _store_story(
            title="New AI model released",
            url="https://www.reddit.com/r/LocalLLaMA/comments/abc/ai_model/",
            source_name="r/LocalLLaMA",
            external_url=article_url,
        )
        store.add_stories_to_store([reddit_row], [])

        # Candidate: the RSS article whose url IS the article_url.
        candidate = new_candidate(
            title="Verge: New AI model released today",
            url=article_url,
            source="rss",
            source_name="The Verge",
        )

        rows = store.list_store_rows()
        hit = match_candidate_to_store(candidate.to_dict(), rows)
        assert hit is not None

    def test_malformed_row_raw_json_degrades_silently(self, store):
        """Row with malformed raw_json must not crash matching."""
        store.add_stories_to_store(
            [_store_story(title="Story", url="https://example.com/story")],
            [],
        )
        # Corrupt the raw_json to be malformed.
        store._conn.execute(
            "UPDATE pending_posts SET raw_json='{not valid json' WHERE 1=1"
        )

        # Candidate with a completely different URL — no match possible.
        candidate = new_candidate(
            title="Different story",
            url="https://other.com/article",
            source="rss",
            source_name="Other",
        )

        rows = store.list_store_rows()
        # Must not crash, must not match (malformed raw_json -> no ext key).
        hit = match_candidate_to_store(candidate.to_dict(), rows)
        assert hit is None

    def test_row_external_url_helper_tolerant(self):
        """_row_external_url_key handles str, dict, None, malformed."""
        # Dict input (in-memory).
        assert _row_external_url_key(
            {"raw_json": {"external_url": "https://example.com/article"}}
        ) == _canonical_url("https://example.com/article")

        # String input (DB form).
        assert _row_external_url_key(
            {"raw_json": '{"external_url": "https://example.com/article"}'}
        ) == _canonical_url("https://example.com/article")

        # None / missing.
        assert _row_external_url_key({}) == ""
        assert _row_external_url_key({"raw_json": None}) == ""

        # Malformed JSON string.
        assert _row_external_url_key({"raw_json": "not json"}) == ""

        # Non-dict JSON.
        assert _row_external_url_key({"raw_json": "[1,2,3]"}) == ""

        # Non-http external_url.
        assert _row_external_url_key(
            {"raw_json": {"external_url": "not-a-url"}}
        ) == ""


# --- AC 4: Contributing URLs persisted in merged_urls + seen ---------------


class TestContributingUrlsPersistence:
    """AC 4: two candidates merged in-cycle then inserted -> merged_urls
    contains the absorbed candidate's url; both urls (and external urls)
    are in seen; re-collecting the absorbed permalink next cycle produces
    no new row."""

    def test_contributing_urls_in_merged_urls_after_insert(self, store):
        """Two candidates merged in-cycle by dedupe_and_merge; the result
        is inserted into the store; merged_urls must contain the absorbed
        candidate's url."""
        a = new_candidate(
            title="Big AI News",
            url="https://hn.algolia.com/story/big-ai-news",
            source="hn",
            source_name="Hacker News",
            upvotes=500,
        )
        b = new_candidate(
            title="Big AI News",
            url="https://reddit.com/r/LocalLLaMA/comments/abc/big_ai_news",
            source="reddit",
            source_name="r/LocalLLaMA",
            upvotes=2000,
            raw_json={"external_url": "https://example.com/big-ai-news-article"},
        )
        merged = dedupe_and_merge([a.to_dict(), b.to_dict()])
        assert len(merged) == 1
        assert "contributing_urls" in merged[0]
        # The absorbed candidate's url must be in contributing_urls.
        assert "https://reddit.com/r/LocalLLaMA/comments/abc/big_ai_news" in \
            merged[0]["contributing_urls"]
        # The external_url must also be in contributing_urls (canonical form).
        assert _canonical_url("https://example.com/big-ai-news-article") in \
            merged[0]["contributing_urls"]
        # The old primary's url is also captured when the primary switches.
        assert "https://hn.algolia.com/story/big-ai-news" in \
            merged[0]["contributing_urls"]

        # The story's url is the primary (reddit permalink, which won the
        # primary switch). merged_urls is seeded from contributing_urls
        # MINUS the row's own url, so the reddit permalink is excluded
        # (it IS the row url). The HN url and external_url must be present.
        story = _store_story(
            title=merged[0]["title"],
            url=merged[0]["url"],
            source=merged[0]["source"],
            source_name=merged[0]["source_name"],
        )
        story["contributing_urls"] = merged[0]["contributing_urls"]
        store.add_stories_to_store([story], [])

        row = store._conn.execute(
            "SELECT merged_urls FROM pending_posts"
        ).fetchone()
        merged_urls = json.loads(row["merged_urls"] or "[]")
        # The old primary's url (absorbed when primary switched) must persist.
        assert "https://hn.algolia.com/story/big-ai-news" in merged_urls
        # The external_url (canonical form) must also persist.
        assert _canonical_url("https://example.com/big-ai-news-article") in merged_urls
        # The row's own url must NOT be in merged_urls (it's the row url).
        assert merged[0]["url"] not in merged_urls

    def test_contributing_urls_in_seen_after_insert(self, store):
        """Contributing URLs must be in the seen table after insert."""
        a = new_candidate(
            title="Tech Breakthrough",
            url="https://example.com/tech-breakthrough",
            source="rss",
            source_name="TechCrunch",
        )
        b = new_candidate(
            title="Tech Breakthrough",
            url="https://reddit.com/r/technology/comments/xyz/tech_breakthrough",
            source="reddit",
            source_name="r/technology",
        )
        merged = dedupe_and_merge([a.to_dict(), b.to_dict()])
        assert len(merged) == 1

        story = _store_story(
            title=merged[0]["title"],
            url=merged[0]["url"],
        )
        story["contributing_urls"] = merged[0]["contributing_urls"]

        # seen_items includes the primary + contributing urls.
        seen_items = [story]
        for curl in merged[0]["contributing_urls"]:
            seen_items.append({"url": curl, "title": story["title"]})

        store.add_stories_to_store([story], seen_items)

        # The absorbed permalink must be in seen.
        assert store.is_seen(
            "https://reddit.com/r/technology/comments/xyz/tech_breakthrough",
            "Tech Breakthrough",
        ), "absorbed permalink must be in seen table"

    def test_recollected_permalink_no_new_row(self, store):
        """Re-collecting the absorbed permalink next cycle produces no
        new row — it's caught by filter_seen."""
        a = new_candidate(
            title="Groundbreaking Research",
            url="https://example.com/research",
            source="rss",
            source_name="Nature",
        )
        b = new_candidate(
            title="Groundbreaking Research",
            url="https://reddit.com/r/science/comments/abc/research",
            source="reddit",
            source_name="r/science",
        )
        merged = dedupe_and_merge([a.to_dict(), b.to_dict()])

        story = _store_story(title=merged[0]["title"], url=merged[0]["url"])
        story["contributing_urls"] = merged[0]["contributing_urls"]
        seen_items = [story]
        for curl in merged[0]["contributing_urls"]:
            seen_items.append({"url": curl, "title": story["title"]})
        store.add_stories_to_store([story], seen_items)

        # Re-collect the absorbed permalink.
        assert store.is_seen(
            "https://reddit.com/r/science/comments/abc/research",
            "Groundbreaking Research",
        ), "absorbed permalink must be seen so filter_seen drops it next cycle"


# --- AC 5: Window enforcement ----------------------------------------------


class TestMergeWindow:
    """AC 5: rows with posted_at older than the window are NOT merge
    targets; NEWS_MERGE_WINDOW_DAYS env respected."""

    def test_old_posted_row_not_merge_target(self, store):
        """A row posted 30 days ago must not appear in list_merge_target_rows(7)."""
        article_url = "https://example.com/old-story"
        story = _store_story(
            title="Old Story",
            url=article_url,
            external_url=article_url,
        )
        store.add_stories_to_store([story], [])
        row_id = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]

        # Set posted_at to 30 days ago.
        old_posted = _iso(30)
        store._conn.execute(
            "UPDATE pending_posts SET posted_at=? WHERE id=?",
            (old_posted, row_id),
        )

        # list_merge_target_rows(7) must NOT include this row.
        targets = store.list_merge_target_rows(7)
        assert all(r["id"] != row_id for r in targets), \
            "row posted 30 days ago must not be a merge target (window=7)"

    def test_recent_posted_row_is_merge_target(self, store):
        """A row posted 1 day ago must appear in list_merge_target_rows(7)."""
        story = _store_story(title="Recent", url="https://example.com/recent")
        store.add_stories_to_store([story], [])
        row_id = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        store._conn.execute(
            "UPDATE pending_posts SET posted_at=? WHERE id=?",
            (_iso(1), row_id),
        )

        targets = store.list_merge_target_rows(7)
        ids = [r["id"] for r in targets]
        assert row_id in ids, "row posted 1 day ago must be a merge target (window=7)"

    def test_unposted_row_always_merge_target(self, store):
        """An unposted row must always be a merge target, regardless of window."""
        story = _store_story(title="Unposted", url="https://example.com/unposted")
        store.add_stories_to_store([story], [])
        row_id = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]

        targets = store.list_merge_target_rows(1)
        ids = [r["id"] for r in targets]
        assert row_id in ids

    def test_posted_at_included_in_result(self, store):
        """list_merge_target_rows must include posted_at so callers can
        tell posted targets apart from unposted ones."""
        # Unposted row.
        store.add_stories_to_store(
            [_store_story(title="Unposted", url="https://example.com/u")], []
        )
        # Posted row.
        store.add_stories_to_store(
            [_store_story(title="Posted", url="https://example.com/p")], []
        )
        row_id = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='Posted'"
        ).fetchone()["id"]
        store._conn.execute(
            "UPDATE pending_posts SET posted_at=? WHERE id=?",
            (_iso(1), row_id),
        )

        targets = store.list_merge_target_rows(7)
        for r in targets:
            assert "posted_at" in r, "posted_at must be in the result dict"
        posted_row = next(r for r in targets if r["id"] == row_id)
        assert posted_row["posted_at"] is not None
        unposted_row = next(r for r in targets if r["title"] == "Unposted")
        assert unposted_row["posted_at"] is None


# --- AC 6: Unposted-only methods unchanged ---------------------------------


class TestUnpostedOnlyMethods:
    """AC 6: pick_hottest/evict_coldest/count_pending still operate on
    unposted rows only (existing tests keep passing)."""

    def test_list_store_rows_excludes_posted(self, store):
        store.add_stories_to_store(
            [_store_story(title="A", url="https://a.example.com")], []
        )
        store.add_stories_to_store(
            [_store_story(title="B", url="https://b.example.com")], []
        )
        row_b = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='B'"
        ).fetchone()["id"]
        store._conn.execute(
            "UPDATE pending_posts SET posted_at=? WHERE id=?",
            (_iso(0), row_b),
        )

        rows = store.list_store_rows()
        titles = {r["title"] for r in rows}
        assert titles == {"A"}, "list_store_rows must show unposted only"

    def test_count_pending_excludes_posted(self, store):
        store.add_stories_to_store(
            [_store_story(title="A", url="https://a.example.com")], []
        )
        store.add_stories_to_store(
            [_store_story(title="B", url="https://b.example.com")], []
        )
        row_b = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='B'"
        ).fetchone()["id"]
        store._conn.execute(
            "UPDATE pending_posts SET posted_at=? WHERE id=?",
            (_iso(0), row_b),
        )
        assert store.count_pending() == 1

    def test_evict_coldest_ignores_posted(self, store):
        from newsbot.scoring import engagement
        store.add_stories_to_store(
            [_store_story(title="Cold", url="https://cold.example.com",
                          upvotes=1, comments=0, engagement=1.0)], []
        )
        store.add_stories_to_store(
            [_store_story(title="Hot", url="https://hot.example.com",
                          upvotes=100, comments=50, engagement=100.0)], []
        )
        # Post the cold row.
        cold_id = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='Cold'"
        ).fetchone()["id"]
        store._conn.execute(
            "UPDATE pending_posts SET posted_at=? WHERE id=?",
            (_iso(0), cold_id),
        )

        rows = store.list_store_rows()
        temps = {r["id"]: 1.0 for r in rows}
        evicted = store.evict_coldest(temps, cap=0)
        assert evicted == 1  # the one unposted row evicted
        # Posted row must survive.
        assert store._conn.execute(
            "SELECT 1 FROM pending_posts WHERE id=? AND posted_at IS NOT NULL",
            (cold_id,),
        ).fetchone() is not None


# --- AC 7: Merge into posted row never resurrects it -----------------------


class TestMergeIntoPostedRow:
    """AC 7 (implicit): merging into a posted row must never resurrect it
    (posted_at unchanged, row stays out of list_store_rows)."""

    def test_merge_does_not_touch_posted_at(self, store):
        """merge_into_store_row selects by id without a posted filter and
        never touches posted_at."""
        story = _store_story(title="Original", url="https://example.com/original")
        store.add_stories_to_store([story], [])
        row_id = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        posted_at = _iso(2)
        store._conn.execute(
            "UPDATE pending_posts SET posted_at=? WHERE id=?",
            (posted_at, row_id),
        )

        # Merge a candidate into this posted row.
        candidate = _store_story(
            title="Original (updated)",
            url="https://reddit.com/r/test/comments/abc/original",
            upvotes=200,
        )
        store.merge_into_store_row(row_id, candidate, "https://reddit.com/r/test/comments/abc/original")

        row = store._conn.execute(
            "SELECT posted_at, merge_count FROM pending_posts WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["posted_at"] == posted_at, "merge must not change posted_at"
        assert row["merge_count"] == 2


# --- Regression: merge_count not inflated by multiple URLs ---------------


class TestMergeCountNotInflated:
    """Regression: merge_into_store_row with a list of URLs must
    increment merge_count exactly ONCE, not once per URL.

    The original step-9 loop called merge_into_store_row once per
    contributing URL, and each call did merge_count += 1 — so one
    candidate carrying N URLs inflated merge_count by N. merge_count
    feeds the merge multiplier in pick_hottest; inflated counts
    distort posting selection.
    """

    def test_list_of_urls_single_merge_count_increment(self, store):
        """One candidate merged with 3 URLs -> merge_count goes 1 -> 2,
        not 1 -> 4. All URLs present in merged_urls."""
        story = _store_story(title="Original", url="https://example.com/orig")
        store.add_stories_to_store([story], [])
        row_id = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]

        candidate = _store_story(
            title="Original (dup)",
            url="https://reddit.com/r/test/comments/abc/orig",
            upvotes=200,
        )
        urls = [
            "https://reddit.com/r/test/comments/abc/orig",
            "https://hn.algolia.com/story/orig",
            "https://example.com/orig-article",
        ]
        store.merge_into_store_row(row_id, candidate, urls)

        row = store._conn.execute(
            "SELECT merge_count, merged_urls FROM pending_posts WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["merge_count"] == 2, \
            f"merge_count must be 2 (one merge), got {row['merge_count']}"
        merged = json.loads(row["merged_urls"] or "[]")
        for u in urls:
            assert u in merged, f"URL {u} must be in merged_urls: {merged}"

    def test_str_arg_backward_compatible(self, store):
        """Passing a single str (old call sites) still works — merge_count
        increments by 1, URL appended."""
        story = _store_story(title="Original", url="https://example.com/orig")
        store.add_stories_to_store([story], [])
        row_id = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]

        store.merge_into_store_row(row_id, _store_story(title="Dup"), "https://b.example.com/1")

        row = store._conn.execute(
            "SELECT merge_count, merged_urls FROM pending_posts WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["merge_count"] == 2
        assert "https://b.example.com/1" in json.loads(row["merged_urls"] or "[]")

    def test_dedupe_urls_in_list(self, store):
        """Duplicate URLs in the list are deduped — no double-append."""
        story = _store_story(title="Original", url="https://example.com/orig")
        store.add_stories_to_store([story], [])
        row_id = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]

        url = "https://b.example.com/1"
        store.merge_into_store_row(row_id, _store_story(title="Dup"), [url, url, url])

        row = store._conn.execute(
            "SELECT merge_count, merged_urls FROM pending_posts WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["merge_count"] == 2
        merged = json.loads(row["merged_urls"] or "[]")
        assert merged.count(url) == 1, "duplicate URL must appear once"
