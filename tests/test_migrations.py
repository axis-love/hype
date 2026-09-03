"""Tests for database migrations and retention policies (flow_001032)."""
import json
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
        # Should have exactly len(_MIGRATIONS) applied, not duplicated.
        from newsbot.db import _MIGRATIONS
        assert rows["n"] == len(_MIGRATIONS)
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
            assert store.count_pending("telegram") == 1
        # Connection should be closed after context exit.
        with pytest.raises(sqlite3.ProgrammingError):
            store._conn.execute("SELECT 1")


class TestRetentionPruning:
    """Verify retention pruning preserves active data and removes old data."""

    def test_prune_delivered_removes_old(self, store):
        """Posted posts older than the cutoff should be removed."""
        # Insert and mark as posted with an old timestamp.
        store.add_pending_post({"title": "Old", "body": "B", "url": ""})
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds")
        post = store.list_unposted_posts("telegram")[0]
        store._conn.execute(
            "INSERT OR IGNORE INTO deliveries(post_id, channel, delivered_at, message_id) "
            "VALUES(?,?,?,?)",
            (post["id"], "telegram", old_ts, None),
        )

        deleted = store.prune_delivered(max_age_days=30)
        assert deleted == 1
        assert store.count_pending("telegram") == 0

    def test_prune_delivered_preserves_recent(self, store):
        """Recently posted posts should NOT be removed."""
        store.add_pending_post({"title": "Recent", "body": "B", "url": ""})
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
        post = store.list_unposted_posts("telegram")[0]
        store._conn.execute("UPDATE pending_posts SET posted_at=? WHERE id=?", (recent_ts, post["id"]))

        deleted = store.prune_delivered(max_age_days=30)
        assert deleted == 0
        # The posted post should still exist.
        rows = store._conn.execute("SELECT * FROM pending_posts").fetchall()
        assert len(rows) == 1

    def test_prune_delivered_preserves_unposted(self, store):
        """Unposted posts should NEVER be removed by prune_delivered."""
        store.add_pending_post({"title": "Unposted", "body": "B", "url": ""})
        # Set a very old created_at to try to trick the pruner.
        old_ts = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat(timespec="seconds")
        store._conn.execute("UPDATE pending_posts SET created_at=? WHERE posted_at IS NULL", (old_ts,))

        deleted = store.prune_delivered(max_age_days=1)
        assert deleted == 0
        assert store.count_pending("telegram") == 1

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
            post = store.list_unposted_posts("telegram")[0]
            store._conn.execute(
                "INSERT OR IGNORE INTO deliveries(post_id, channel, delivered_at, message_id) "
                "VALUES(?,?,?,?)",
                (post["id"], "telegram", old_ts, None),
            )

        # Prune with small batch size.
        deleted = store.prune_delivered(max_age_days=30, batch_size=10)
        assert deleted == 100
        assert store.count_pending("telegram") == 0

# --- flow_001040: persist score components in pending_posts ---


class TestScoreColumnsMigration:
    """Verify migration 3 adds score columns to pending_posts."""

    def test_score_columns_exist(self, store):
        """All score columns should exist after migration 3."""
        cols = store._conn.execute("PRAGMA table_info(pending_posts)").fetchall()
        col_names = {c["name"] for c in cols}
        expected = {
            "source", "published_at", "upvotes", "comments", "stars",
            "reposts", "crosspost_count", "penalty", "lookback_hours",
            "score_at_queue", "engagement_score", "recency_at_queue",
            "source_weight", "topic_bonus", "crosspost_bonus",
            "matched_topics", "scored_at",
        }
        missing = expected - col_names
        assert not missing, f"Missing columns: {missing}"

    def test_legacy_rows_have_null_scores(self, store):
        """Rows inserted before migration 3 should have NULL score columns."""
        # Insert a post the old way (no score data).
        store.add_pending_post({"title": "Legacy", "body": "B", "url": ""})
        row = store._conn.execute(
            "SELECT score_at_queue, engagement_score, scored_at FROM pending_posts WHERE title='Legacy'"
        ).fetchone()
        assert row["score_at_queue"] is None
        assert row["engagement_score"] is None
        assert row["scored_at"] is None

    def test_upgrade_preserves_existing_rows(self, tmp_path):
        """Upgrading a v2 DB with pending rows should preserve them with NULL score columns."""
        db_path = tmp_path / "test.sqlite"
        # Manually create a v2 database (only migrations 1-2 applied).
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE schema_version(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT);
            INSERT INTO schema_version VALUES(1, 'Initial schema', '2026-01-01T00:00:00+00:00');
            INSERT INTO schema_version VALUES(2, 'Drop unused tables', '2026-01-01T00:00:00+00:00');
            CREATE TABLE pending_posts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, body TEXT NOT NULL,
                category TEXT, importance INTEGER, url TEXT,
                created_at TEXT NOT NULL, posted_at TEXT
            );
            INSERT INTO pending_posts(title, body, url, created_at) VALUES('Pre-existing', 'B', 'https://example.com', '2026-01-01T00:00:00+00:00');
            CREATE TABLE seen(url TEXT PRIMARY KEY, title TEXT, first_seen_at TEXT NOT NULL);
        """)
        conn.commit()
        conn.close()

        # Reopen with NewsStore — migrations 3+4+5 should apply and preserve the row.
        store2 = NewsStore(db_path)
        rows = store2._conn.execute("SELECT * FROM pending_posts WHERE title='Pre-existing'").fetchall()
        assert len(rows) == 1
        assert rows[0]["score_at_queue"] is None
        assert rows[0]["scored_at"] is None
        # Migration 4 backfills legacy rows with merge_count=1 (additive default).
        assert rows[0]["merge_count"] == 1
        # Migration 5 adds message_id (NULL for legacy rows).
        assert rows[0]["message_id"] is None
        # Migration 6 adds origin_topic (NULL for legacy rows).
        assert rows[0]["origin_topic"] is None
        # Migration 7 creates deliveries table. This row has posted_at
        # NULL so backfill skips it — no delivery row should exist.
        del_count = store2._conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE post_id=?", (rows[0]["id"],)
        ).fetchone()
        assert del_count["n"] == 0
        # Verify all migrations were applied.
        version_row = store2._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        assert version_row["v"] == 7
        store2.close()


class TestAddStoriesToStoreWithScores:
    """Verify add_stories_to_store stores score components (v2 successor of
    TestReplaceUnpostedBatchWithScores — folded into test_store.py coverage,
    kept here for the migration-3 column contract)."""

    def test_add_stores_score_components(self, store):
        """add_stories_to_store should store score_breakdown data in columns."""
        from datetime import datetime, timezone
        bd = {
            "score": 123.4,
            "engagement": 100.0,
            "recency": 0.88,
            "source_weight": 1.2,
            "topic_bonus": 20,
            "crosspost_bonus": 30.0,
            "penalty": 1.0,
            "matched_topics": ["ai", "llm"],
            "scored_at": "2026-07-28T12:00:00+00:00",
            "lookback_hours": 48,
            "source": "hn",
            "published_at": "2026-07-28T06:00:00+00:00",
            "upvotes": 420,
            "comments": 88,
            "stars": 0,
            "reposts": 0,
            "crosspost_count": 2,
        }
        post = {
            "title": "Test Post",
            "body": "Body text",
            "url": "https://example.com",
            "candidate_id": "c001",
            "score_breakdown": bd,
        }
        inserted = store.add_stories_to_store([post], [])
        assert inserted == 1

        row = store._conn.execute(
            "SELECT * FROM pending_posts WHERE title='Test Post'"
        ).fetchone()
        assert row["score_at_queue"] == 123.4
        assert row["engagement_score"] == 100.0
        assert row["recency_at_queue"] == 0.88
        assert row["source_weight"] == 1.2
        assert row["topic_bonus"] == 20
        assert row["crosspost_bonus"] == 30.0
        assert row["penalty"] == 1.0
        assert row["source"] == "hn"
        assert row["upvotes"] == 420
        assert row["comments"] == 88
        assert row["crosspost_count"] == 2
        assert row["lookback_hours"] == 48
        assert row["scored_at"] == "2026-07-28T12:00:00+00:00"
        # matched_topics stored as JSON
        import json
        matched = json.loads(row["matched_topics"])
        assert matched == ["ai", "llm"]

    def test_add_without_score_breakdown(self, store):
        """Stories without score_breakdown should still insert (NULL score columns)."""
        post = {"title": "No Scores", "body": "B", "url": "https://example.com"}
        inserted = store.add_stories_to_store([post], [])
        assert inserted == 1
        row = store._conn.execute(
            "SELECT score_at_queue, scored_at FROM pending_posts WHERE title='No Scores'"
        ).fetchone()
        assert row["score_at_queue"] is None
        assert row["scored_at"] is None

    def test_matched_topics_json_roundtrip(self, store):
        """matched_topics should round-trip through JSON correctly."""
        bd = {
            "matched_topics": ["ai", "llm", "local_llm"],
            "score": 50.0,
        }
        post = {"title": "JSON Test", "body": "B", "url": "", "score_breakdown": bd}
        store.add_stories_to_store([post], [])
        row = store._conn.execute(
            "SELECT matched_topics FROM pending_posts WHERE title='JSON Test'"
        ).fetchone()
        import json
        matched = json.loads(row["matched_topics"])
        assert matched == ["ai", "llm", "local_llm"]


class TestListUnpostedPosts:
    """Verify list_unposted_posts returns correct ordering."""

    def test_empty_queue(self, store):
        """Empty queue should return empty list."""
        assert store.list_unposted_posts("telegram") == []

    def test_ordering_oldest_first(self, store):
        """Posts should be ordered by created_at, id (oldest first)."""
        store.add_pending_post({"title": "First", "body": "B", "url": ""})
        store.add_pending_post({"title": "Second", "body": "B", "url": ""})
        store.add_pending_post({"title": "Third", "body": "B", "url": ""})
        posts = store.list_unposted_posts("telegram")
        assert len(posts) == 3
        assert posts[0]["title"] == "First"
        assert posts[1]["title"] == "Second"
        assert posts[2]["title"] == "Third"

    def test_excludes_posted(self, store):
        """Posted posts should not appear in list_unposted_posts."""
        store.add_pending_post({"title": "Unposted", "body": "B", "url": ""})
        store.add_pending_post({"title": "AlsoUnposted", "body": "B", "url": ""})
        post = store.list_unposted_posts("telegram")[0]
        store.mark_posted(post["id"])
        unposted = store.list_unposted_posts("telegram")
        assert len(unposted) == 1
        assert unposted[0]["title"] == "AlsoUnposted"
