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