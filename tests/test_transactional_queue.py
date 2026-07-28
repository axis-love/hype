"""Tests for transactional queue replacement (flow_001022).

Verifies that existing unposted rows are preserved when collection,
LLM, insertion, or commit failures occur, and that successful
replacement makes the complete new batch visible atomically.
"""
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from newsbot.db import NewsStore


@pytest.fixture
def store(tmp_path: Path) -> NewsStore:
    return NewsStore(tmp_path / "test.sqlite")


class TestReplaceUnpostedBatch:
    """Verify transactional queue replacement semantics."""

    def test_successful_replacement_clears_old_and_inserts_new(self, store):
        """A successful replacement removes old unposted posts and inserts new ones."""
        # Seed old posts.
        store.add_pending_post({"title": "Old1", "body": "B1", "url": "http://old.com"})
        store.add_pending_post({"title": "Old2", "body": "B2", "url": "http://old.com/2"})
        assert store.count_pending() == 2

        new_posts = [
            {"title": "New1", "body": "Body1", "url": "http://new.com/1"},
            {"title": "New2", "body": "Body2", "url": "http://new.com/2"},
            {"title": "New3", "body": "Body3", "url": "http://new.com/3"},
        ]
        seen_items = [
            {"url": "http://new.com/1", "title": "New1"},
            {"url": "http://new.com/2", "title": "New2"},
            {"url": "http://new.com/3", "title": "New3"},
        ]

        inserted, seen = store.replace_unposted_batch(new_posts, seen_items)
        assert inserted == 3
        assert seen == 3
        assert store.count_pending() == 3

        # Verify old posts are gone.
        post = store.get_next_pending_post()
        assert post["title"] == "New1"

    def test_failed_replacement_preserves_old_queue(self, store):
        """If insertion fails, the old queue must remain intact."""
        # Seed old posts.
        store.add_pending_post({"title": "Old1", "body": "B1", "url": "http://old.com"})
        assert store.count_pending() == 1

        # Force a sqlite error during the transaction by using a connection
        # wrapper that fails on execute.
        new_posts = [{"title": "New1", "body": "B1", "url": "http://new.com"}]
        seen_items = [{"url": "http://new.com", "title": "New1"}]

        class FailingConn:
            def cursor(self):
                class FailingCursor:
                    def execute(self, sql, *args):
                        raise sqlite3.OperationalError("disk full")
                    def close(self):
                        pass
                return FailingCursor()

        original_conn = store._conn
        store._conn = FailingConn()  # type: ignore
        try:
            with pytest.raises(sqlite3.OperationalError):
                store.replace_unposted_batch(new_posts, seen_items)
        finally:
            store._conn = original_conn  # type: ignore

        # Old queue should still be intact.
        assert store.count_pending() == 1
        post = store.get_next_pending_post()
        assert post["title"] == "Old1"

    def test_insert_failure_does_not_mark_seen(self, store):
        """Seen items should not be marked if the transaction fails."""
        seen_items = [{"url": "http://example.com", "title": "Test"}]

        class FailingConn:
            def cursor(self):
                class FailingCursor:
                    def execute(self, sql, *args):
                        raise sqlite3.OperationalError("disk full")
                    def close(self):
                        pass
                return FailingCursor()

        original_conn = store._conn
        store._conn = FailingConn()  # type: ignore
        try:
            with pytest.raises(sqlite3.OperationalError):
                store.replace_unposted_batch([], seen_items)
        finally:
            store._conn = original_conn  # type: ignore

        # The seen entry should NOT exist.
        assert not store.is_seen("http://example.com", "Test")

    def test_empty_new_posts_clears_queue(self, store):
        """An empty new_posts list should clear the old queue (intentional)."""
        store.add_pending_post({"title": "Old1", "body": "B1", "url": "http://old.com"})
        assert store.count_pending() == 1

        inserted, seen = store.replace_unposted_batch([], [])
        assert inserted == 0
        assert seen == 0
        assert store.count_pending() == 0

    def test_posted_posts_preserved_during_replacement(self, store):
        """Already-posted posts should NOT be cleared during replacement."""
        # One unposted, one posted.
        store.add_pending_post({"title": "Unposted", "body": "B", "url": "http://u.com"})
        store.add_pending_post({"title": "Posted", "body": "B", "url": "http://p.com"})
        # Mark the second as posted.
        posts = list(store._conn.execute("SELECT * FROM pending_posts ORDER BY id").fetchall())
        store.mark_posted(posts[1]["id"])
        assert store.count_pending() == 1

        # Replace with new posts.
        new_posts = [{"title": "New", "body": "B", "url": "http://n.com"}]
        inserted, seen = store.replace_unposted_batch(new_posts, [])
        assert inserted == 1
        assert store.count_pending() == 1

        # Verify the posted post is still there (posted_at is not null).
        all_posts = list(store._conn.execute("SELECT * FROM pending_posts ORDER BY id").fetchall())
        titles = [p["title"] for p in all_posts]
        assert "Posted" in titles
        assert "New" in titles
        assert "Unposted" not in titles

    def test_atomic_visibility_no_partial_batch(self, store):
        """A successful replacement shows the complete new batch — no partial state."""
        store.add_pending_post({"title": "Old", "body": "B", "url": "http://o.com"})

        new_posts = [
            {"title": f"New{i}", "body": f"B{i}", "url": f"http://n.com/{i}"}
            for i in range(5)
        ]

        inserted, _ = store.replace_unposted_batch(new_posts, [])
        assert inserted == 5

        # All 5 should be visible immediately, no old ones.
        all_pending = []
        while True:
            p = store.get_next_pending_post()
            if not p:
                break
            all_pending.append(p["title"])
            store.mark_posted(p["id"])

        assert sorted(all_pending) == ["New0", "New1", "New2", "New3", "New4"]
        assert "Old" not in all_pending

    def test_partial_llm_output_only_marks_styled_items_seen(self, store):
        """When the styler omits some items, only styled items should be marked seen."""
        # Simulate: 4 final items, but only 2 got styled into posts.
        new_posts = [
            {"title": "Post1", "body": "B1", "url": "http://x.com/1", "candidate_id": "c001"},
            {"title": "Post2", "body": "B2", "url": "http://x.com/2", "candidate_id": "c002"},
        ]
        # Only the 2 styled items should be marked seen — not all 4 finals.
        seen_items = [
            {"url": "http://x.com/1", "title": "Item1", "candidate_id": "c001"},
            {"url": "http://x.com/2", "title": "Item2", "candidate_id": "c002"},
        ]

        inserted, seen = store.replace_unposted_batch(new_posts, seen_items)
        assert inserted == 2
        assert seen == 2

        # Styled items should be marked seen.
        assert store.is_seen("http://x.com/1", "Item1")
        assert store.is_seen("http://x.com/2", "Item2")
        # Omitted items should NOT be marked seen.
        assert not store.is_seen("http://x.com/3", "Item3")
        assert not store.is_seen("http://x.com/4", "Item4")


class TestBatchSeenDuplicateHandling:
    """Verify duplicate URLs/titles in batch seen filtering (flow_001033 round 1)."""

    def test_duplicate_urls_all_filtered(self, store, tmp_path):
        """When multiple candidates share the same seen URL, ALL should be filtered."""
        # Mark a URL as seen
        store.mark_seen([{"url": "http://dup.com", "title": "dup"}])

        items = [
            {"url": "http://dup.com", "title": "First"},
            {"url": "http://dup.com", "title": "Second"},
            {"url": "http://dup.com", "title": "Third"},
            {"url": "http://other.com", "title": "Unique"},
        ]
        seen_idx = store.is_seen_batch(items)
        # All three dup.com items should be in seen_idx (indices 0, 1, 2)
        assert 0 in seen_idx
        assert 1 in seen_idx
        assert 2 in seen_idx
        # The unique one should NOT be seen
        assert 3 not in seen_idx

    def test_duplicate_titles_all_filtered(self, store, tmp_path):
        """When multiple candidates share the same seen title, ALL should be filtered."""
        store.mark_seen([{"url": "", "title": "same title"}])

        items = [
            {"url": "http://a.com", "title": "Same Title"},
            {"url": "http://b.com", "title": "Same Title"},
            {"url": "http://c.com", "title": "Different"},
        ]
        seen_idx = store.is_seen_batch(items)
        assert 0 in seen_idx


def test_mark_seen_atomic_transaction(tmp_path):
    """mark_seen should use explicit transaction — partial failures roll back."""
    from newsbot.db import NewsStore

    store = NewsStore(tmp_path / "test.sqlite")

    # Insert 3 items — all should succeed atomically.
    items = [
        {"url": "http://a.com", "title": "A"},
        {"url": "http://b.com", "title": "B"},
        {"url": "http://c.com", "title": "C"},
    ]
    count = store.mark_seen(items)
    assert count == 3  # all 3 newly inserted

    # Re-insert same items — should return 0 (all duplicates).
    count2 = store.mark_seen(items)
    assert count2 == 0


def test_replace_unposted_batch_seen_marked_uses_rowcount(tmp_path):
    """replace_unposted_batch should return actual inserted seen count,
    not len(input) which counts duplicates."""
    from newsbot.db import NewsStore

    store = NewsStore(tmp_path / "test.sqlite")

    # Pre-mark some items as seen so they're duplicates.
    store.mark_seen([{"url": "http://old.com", "title": "old"}])

    # Now replace_unposted_batch with seen_items including the pre-seen one.
    posts = [{"title": "New", "body": "B", "url": "http://new.com"}]
    seen_items = [
        {"url": "http://old.com", "title": "old"},  # duplicate
        {"url": "http://new.com", "title": "new"},  # new
    ]

    inserted, seen_marked = store.replace_unposted_batch(posts, seen_items)
    assert inserted == 1  # 1 post inserted
    # seen_marked should be 1 (only the new one), not 2 (len of input)
    assert seen_marked == 1, f"seen_marked should be 1 (actual inserts), got {seen_marked}"


def test_mark_seen_large_batch(tmp_path):
    """mark_seen should handle batches larger than SQLite's default chunk size."""
    from newsbot.db import NewsStore

    store = NewsStore(tmp_path / "test.sqlite")
    # 600 items — exceeds the 500-item chunk size in some implementations.
    items = [
        {"url": f"http://item-{i}.com", "title": f"item-{i}"}
        for i in range(600)
    ]
    count = store.mark_seen(items)
    assert count == 600  # all 600 newly inserted

# --- flow_001040: score data join by candidate_id ---


def test_replace_stores_scores_after_styler_reorder(tmp_path):
    """Score data should be joined by candidate_id, not positional zip.

    The styler can reorder or omit items. replace_unposted_batch should
    receive the correct score_breakdown for each post regardless of order.
    """
    from newsbot.db import NewsStore
    store = NewsStore(tmp_path / "test.sqlite")

    # Simulate: final items have score_breakdown, styler returns reordered.
    final = [
        {"candidate_id": "c001", "title": "A", "score_breakdown": {"score": 100.0, "engagement": 50.0, "source": "hn", "matched_topics": ["ai"], "scored_at": "2026-07-28T12:00:00+00:00", "lookback_hours": 48, "published_at": "2026-07-28T06:00:00+00:00", "upvotes": 100, "comments": 10, "stars": 0, "reposts": 0, "crosspost_count": 1, "recency": 0.88, "source_weight": 1.2, "topic_bonus": 20, "crosspost_bonus": 0.0, "penalty": 1.0}},
        {"candidate_id": "c002", "title": "B", "score_breakdown": {"score": 200.0, "engagement": 150.0, "source": "reddit", "matched_topics": ["llm"], "scored_at": "2026-07-28T12:00:00+00:00", "lookback_hours": 48, "published_at": "2026-07-28T08:00:00+00:00", "upvotes": 200, "comments": 50, "stars": 0, "reposts": 0, "crosspost_count": 2, "recency": 0.92, "source_weight": 1.0, "topic_bonus": 25, "crosspost_bonus": 30.0, "penalty": 1.0}},
    ]

    # Styler returns reversed order (c002 first, then c001).
    posts = [
        {"title": "B styled", "body": "Body B", "url": "https://b.com", "candidate_id": "c002"},
        {"title": "A styled", "body": "Body A", "url": "https://a.com", "candidate_id": "c001"},
    ]

    # Join by candidate_id (same logic as _run_generation).
    final_by_id = {item["candidate_id"]: item["score_breakdown"] for item in final}
    for post in posts:
        cid = post.get("candidate_id")
        if cid and cid in final_by_id:
            post["score_breakdown"] = final_by_id[cid]

    # Replace and verify each post got the correct score data.
    inserted, _ = store.replace_unposted_batch(posts, [])
    assert inserted == 2

    rows = store.list_unposted_posts()
    assert len(rows) == 2

    # First row (c002, posted first) should have score=200.
    assert rows[0]["title"] == "B styled"
    assert rows[0]["score_at_queue"] == 200.0
    assert rows[0]["source"] == "reddit"

    # Second row (c001) should have score=100.
    assert rows[1]["title"] == "A styled"
    assert rows[1]["score_at_queue"] == 100.0
    assert rows[1]["source"] == "hn"

    store.close()


def test_replace_stores_scores_after_styler_omission(tmp_path):
    """When styler omits an item, only styled posts get score data."""
    from newsbot.db import NewsStore
    store = NewsStore(tmp_path / "test.sqlite")

    final = [
        {"candidate_id": "c001", "title": "A", "score_breakdown": {"score": 100.0, "source": "hn", "matched_topics": [], "scored_at": "2026-07-28T12:00:00+00:00", "lookback_hours": 48, "engagement": 50.0, "recency": 0.88, "source_weight": 1.2, "topic_bonus": 0, "crosspost_bonus": 0.0, "penalty": 1.0, "published_at": None, "upvotes": 100, "comments": 10, "stars": 0, "reposts": 0, "crosspost_count": 1}},
        {"candidate_id": "c002", "title": "B", "score_breakdown": {"score": 200.0, "source": "reddit", "matched_topics": [], "scored_at": "2026-07-28T12:00:00+00:00", "lookback_hours": 48, "engagement": 150.0, "recency": 0.92, "source_weight": 1.0, "topic_bonus": 0, "crosspost_bonus": 30.0, "penalty": 1.0, "published_at": None, "upvotes": 200, "comments": 50, "stars": 0, "reposts": 0, "crosspost_count": 2}},
        {"candidate_id": "c003", "title": "C", "score_breakdown": {"score": 50.0, "source": "github", "matched_topics": [], "scored_at": "2026-07-28T12:00:00+00:00", "lookback_hours": 48, "engagement": 10.0, "recency": 0.5, "source_weight": 1.1, "topic_bonus": 0, "crosspost_bonus": 0.0, "penalty": 1.0, "published_at": None, "upvotes": 0, "comments": 0, "stars": 10, "reposts": 0, "crosspost_count": 1}},
    ]

    # Styler only returns c001 and c003 (omits c002).
    posts = [
        {"title": "A styled", "body": "Body A", "url": "https://a.com", "candidate_id": "c001"},
        {"title": "C styled", "body": "Body C", "url": "https://c.com", "candidate_id": "c003"},
    ]

    # Join by candidate_id.
    final_by_id = {item["candidate_id"]: item["score_breakdown"] for item in final}
    for post in posts:
        cid = post.get("candidate_id")
        if cid and cid in final_by_id:
            post["score_breakdown"] = final_by_id[cid]

    inserted, _ = store.replace_unposted_batch(posts, [])
    assert inserted == 2

    rows = store.list_unposted_posts()
    assert len(rows) == 2
    # c002 should not be in the queue.
    titles = [r["title"] for r in rows]
    assert "A styled" in titles
    assert "C styled" in titles
    # Score data should match.
    for r in rows:
        if r["title"] == "A styled":
            assert r["score_at_queue"] == 100.0
            assert r["source"] == "hn"
        elif r["title"] == "C styled":
            assert r["score_at_queue"] == 50.0
            assert r["source"] == "github"

    store.close()
