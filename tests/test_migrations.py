"""Tests for database migrations and retention policies (flow_001032)."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from newsbot.db import NewsStore


@pytest.fixture
def store(tmp_path: Path) -> NewsStore:
    return NewsStore(tmp_path / "test.sqlite")


class TestMigrations:
    """Verify schema version tracking and migration safety."""

    def test_schema_version_table_created(self, store):
        """schema_version table should exist after init."""
        row = store._conn.execute(
            "SELECT version, description FROM schema_version ORDER BY version"
        ).fetchall()
        assert len(row) >= 1
        assert row[0]["version"] == 1
        assert "Initial schema" in row[0]["description"]

    def test_migrations_are_idempotent(self, tmp_path):
        """Reopening an existing DB should not re-apply migrations."""
        db_path = tmp_path / "test.sqlite"
        store1 = NewsStore(db_path)
        store1.close()

        store2 = NewsStore(db_path)
        rows = store2._conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()
        # Should have exactly 1 migration applied, not duplicated.
        assert rows["n"] == 2  # 2 migrations applied
        store2.close()

    def test_tables_exist_after_migration(self, store):
        """All expected tables should exist after migration (dead tables dropped in migration 2)."""
        for table in ("seen", "pending_posts", "schema_version"):
            row = store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            assert row is not None, f"Table {table} not found"
        # Dead tables should be dropped by migration 2.
        for dead in ("news_items", "news_digests"):
            row = store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (dead,)
            ).fetchone()
            assert row is None, f"Dead table {dead} should be dropped"

    def test_connection_close(self, tmp_path):
        """close() should close the connection without error."""
        store = NewsStore(tmp_path / "test.sqlite")
        store.close()
        # Operating on a closed connection should raise.
        with pytest.raises(sqlite3.ProgrammingError):
            store._conn.execute("SELECT 1")

    def test_context_manager(self, tmp_path):
        """NewsStore should work as a context manager."""
        db_path = tmp_path / "test.sqlite"
        with NewsStore(db_path) as store:
            store.add_pending_post({"title": "T", "body": "B", "url": ""})
            assert store.count_pending() == 1
        # Connection should be closed after context exit.
        with pytest.raises(sqlite3.ProgrammingError):
            store._conn.execute("SELECT 1")


class TestRetentionPruning:
    """Verify retention pruning preserves active data and removes old data."""

    def test_prune_posted_posts_removes_old(self, store):
        """Posted posts older than the cutoff should be removed."""
        # Insert and mark as posted with an old timestamp.
        store.add_pending_post({"title": "Old", "body": "B", "url": ""})
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds")
        post = store.get_next_pending_post()
        store._conn.execute("UPDATE pending_posts SET posted_at=? WHERE id=?", (old_ts, post["id"]))

        deleted = store.prune_posted_posts(max_age_days=30)
        assert deleted == 1
        assert store.count_pending() == 0

    def test_prune_posted_posts_preserves_recent(self, store):
        """Recently posted posts should NOT be removed."""
        store.add_pending_post({"title": "Recent", "body": "B", "url": ""})
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
        post = store.get_next_pending_post()
        store._conn.execute("UPDATE pending_posts SET posted_at=? WHERE id=?", (recent_ts, post["id"]))

        deleted = store.prune_posted_posts(max_age_days=30)
        assert deleted == 0
        # The posted post should still exist.
        rows = store._conn.execute("SELECT * FROM pending_posts").fetchall()
        assert len(rows) == 1

    def test_prune_posted_posts_preserves_unposted(self, store):
        """Unposted posts should NEVER be removed by prune_posted_posts."""
        store.add_pending_post({"title": "Unposted", "body": "B", "url": ""})
        # Set a very old created_at to try to trick the pruner.
        old_ts = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat(timespec="seconds")
        store._conn.execute("UPDATE pending_posts SET created_at=? WHERE posted_at IS NULL", (old_ts,))

        deleted = store.prune_posted_posts(max_age_days=1)
        assert deleted == 0
        assert store.count_pending() == 1

    def test_prune_seen_removes_old(self, store):
        """Seen entries older than the cutoff should be removed."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        store._conn.execute(
            "INSERT INTO seen(url, title, first_seen_at) VALUES(?,?,?)",
            ("http://old.com", "old title", old_ts),
        )

        deleted = store.prune_seen(max_age_days=14)
        assert deleted == 1
        # Entry should be gone.
        row = store._conn.execute("SELECT 1 FROM seen WHERE url=?", ("http://old.com",)).fetchone()
        assert row is None

    def test_prune_seen_preserves_recent(self, store):
        """Recent seen entries should NOT be removed."""
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        store._conn.execute(
            "INSERT INTO seen(url, title, first_seen_at) VALUES(?,?,?)",
            ("http://recent.com", "recent title", recent_ts),
        )

        deleted = store.prune_seen(max_age_days=14)
        assert deleted == 0
        # Entry should still exist.
        row = store._conn.execute("SELECT 1 FROM seen WHERE url=?", ("http://recent.com",)).fetchone()
        assert row is not None

    def test_prune_digests_removed(self, store):
        """prune_digests should not exist — it was removed (table dropped in migration 2)."""
        assert not hasattr(store, "prune_digests"), "prune_digests should be removed"

    def test_batched_pruning(self, store):
        """Large number of rows should be pruned in batches without long locks."""
        # Insert 100 old posted posts.
        old_ts = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat(timespec="seconds")
        for i in range(100):
            store.add_pending_post({"title": f"Old{i}", "body": "B", "url": ""})
            post = store.get_next_pending_post()
            store._conn.execute("UPDATE pending_posts SET posted_at=? WHERE id=?", (old_ts, post["id"]))

        # Prune with small batch size.
        deleted = store.prune_posted_posts(max_age_days=30, batch_size=10)
        assert deleted == 100
        assert store.count_pending() == 0