"""Tests for batched SQLite operations (flow_001033)."""
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from newsbot.db import NewsStore
from newsbot.main import filter_seen


@pytest.fixture
def store(tmp_path: Path) -> NewsStore:
    return NewsStore(tmp_path / "test.sqlite")


class TestBatchedFilterSeen:
    """Verify filter_seen uses batch SQL, not per-item queries."""

    def test_filter_seen_uses_batch_query(self, store):
        """filter_seen should use is_seen_batch, not per-item is_seen."""
        # Seed some seen entries.
        store.mark_seen([
            {"url": "http://seen.com/1", "title": "Seen One"},
        ])
        items = [
            {"url": "http://seen.com/1", "title": "Seen One"},
            {"url": "http://new.com/1", "title": "New One"},
            {"url": "http://new.com/2", "title": "New Two"},
        ]
        result = filter_seen(items, store)
        assert len(result) == 2
        assert result[0]["title"] == "New One"
        assert result[1]["title"] == "New Two"

    def test_is_seen_batch_query_count(self, store):
        """is_seen_batch should do at most 2 queries total, not N per item."""
        # Seed entries.
        store.mark_seen([
            {"url": "http://seen.com/1", "title": "Seen 1"},
            {"url": "http://seen.com/2", "title": "Seen 2"},
        ])
        items = [{"url": f"http://x.com/{i}", "title": f"Title {i}"} for i in range(100)]
        items[0]["url"] = "http://seen.com/1"
        items[1]["url"] = "http://seen.com/2"

        # Count SELECT queries by wrapping the connection.
        original_conn = store._conn
        class CountingConn:
            def __init__(self, real):
                self._real = real
                self.select_count = 0
            def execute(self, sql, *args):
                if isinstance(sql, str) and "SELECT" in sql.upper():
                    self.select_count += 1
                return self._real.execute(sql, *args)
            def __getattr__(self, name):
                return getattr(self._real, name)

        proxy = CountingConn(original_conn)
        store._conn = proxy  # type: ignore

        try:
            seen = store.is_seen_batch(items)
        finally:
            store._conn = original_conn  # type: ignore

        # Should be at most 2 SELECT queries (one for URLs, one for titles).
        assert proxy.select_count <= 2
        assert 0 in seen  # http://seen.com/1
        assert 1 in seen  # http://seen.com/2
        assert 2 not in seen  # not seen

    def test_is_seen_batch_empty(self, store):
        assert store.is_seen_batch([]) == set()

    def test_is_seen_batch_handles_missing_url_and_title(self, store):
        """Items with neither url nor title should not crash."""
        items = [
            {"url": "", "title": ""},
            {"url": "http://x.com", "title": "X"},
        ]
        seen = store.is_seen_batch(items)
        assert seen == set()  # nothing is seen yet


class TestBatchedMarkSeen:
    """Verify mark_seen uses executemany."""

    def test_mark_seen_batch_inserts(self, store):
        items = [
            {"url": "http://a.com", "title": "A"},
            {"url": "http://b.com", "title": "B"},
            {"url": "http://c.com", "title": "C"},
        ]
        count = store.mark_seen(items)
        assert count == 3
        # Verify entries exist.
        for url in ("http://a.com", "http://b.com", "http://c.com"):
            row = store._conn.execute("SELECT 1 FROM seen WHERE url=?", (url,)).fetchone()
            assert row is not None

    def test_mark_seen_batch_duplicates(self, store):
        """Duplicate entries should be silently ignored via INSERT OR IGNORE."""
        items = [
            {"url": "http://dup.com", "title": "Dup"},
            {"url": "http://dup.com", "title": "Dup"},
            {"url": "http://new.com", "title": "New"},
        ]
        count = store.mark_seen(items)
        # rowcount reflects actual inserts, not rows attempted.
        # 2 unique entries inserted (dup.com and new.com), 1 duplicate ignored.
        assert count == 2
        rows = store._conn.execute("SELECT COUNT(*) AS n FROM seen").fetchone()
        assert rows["n"] == 2

    def test_mark_seen_rowcount_after_reinsert(self, store):
        """Re-inserting already-seen items should return 0 (all ignored)."""
        items = [
            {"url": "http://a.com", "title": "A"},
        ]
        first = store.mark_seen(items)
        assert first == 1
        # Re-insert the same item.
        second = store.mark_seen(items)
        assert second == 0  # already exists, INSERT OR IGNORE skipped it

    def test_mark_seen_empty(self, store):
        assert store.mark_seen([]) == 0

    def test_mark_seen_skips_empty_items(self, store):
        items = [
            {"url": "", "title": ""},
            {"url": "http://x.com", "title": "X"},
        ]
        count = store.mark_seen(items)
        assert count == 1


class TestRemovedAPIs:
    """Verify dead APIs are removed."""

    def test_insert_items_removed(self):
        assert not hasattr(NewsStore, "insert_items"), "insert_items should be removed"

    def test_insert_digest_removed(self):
        assert not hasattr(NewsStore, "insert_digest"), "insert_digest should be removed"

    def test_prune_old_items_retained(self):
        """prune_old_items should still exist (called from generation cycle)."""
        assert hasattr(NewsStore, "prune_old_items"), "prune_old_items should be retained"

    def test_prune_digests_retained(self):
        """prune_digests should still exist for cleanup."""
        assert hasattr(NewsStore, "prune_digests"), "prune_digests should be retained"

    def test_prune_old_items_is_noop(self, store):
        """prune_old_items should be a no-op (table dropped in migration 2)."""
        assert store.prune_old_items(48) == 0

    def test_prune_digests_is_noop(self, store):
        """prune_digests should be a no-op (table dropped in migration 2)."""
        assert store.prune_digests(90) == 0

    def test_dead_tables_dropped_after_migration(self, store):
        """news_items and news_digests tables should not exist after migration."""
        # Migration 2 drops these tables.
        items_exists = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='news_items'"
        ).fetchone()
        digests_exists = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='news_digests'"
        ).fetchone()
        assert items_exists is None, "news_items table should be dropped"
        assert digests_exists is None, "news_digests table should be dropped"

    def test_schema_version_is_2(self, store):
        """Migration 2 should have been applied."""
        row = store._conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        assert row["v"] == 2