"""Tests for migration 7: deliveries table + mark_delivered (flow_001138).

Acceptance criteria:
1. Migration applies on a fresh DB and on a v6 DB with posted rows;
   schema_version = 7.
2. Backfilled row count equals COUNT(*) WHERE posted_at IS NOT NULL.
3. mark_delivered twice for the same (post_id, channel) leaves one row
   and does not raise.
4. mark_posted still sets posted_at and creates the telegram delivery.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from newsbot.db import NewsStore


@pytest.fixture
def store(tmp_path: Path) -> NewsStore:
    return NewsStore(tmp_path / "test.sqlite")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- AC 1: Migration applies on fresh DB and on v6 DB with posted rows ---


class TestMigrationApplies:
    """Migration 7 applies on a fresh DB and on a v6 DB with posted rows."""

    def test_fresh_db_schema_version_7(self, store):
        """A fresh DB should reach schema_version 7 after init."""
        row = store._conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        assert row["v"] == 7

    def test_deliveries_table_exists(self, store):
        """The deliveries table should exist after migration 7."""
        row = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='deliveries'"
        ).fetchone()
        assert row is not None

    def test_deliveries_indexes_exist(self, store):
        """Indexes on post_id, channel, delivered_at should exist."""
        idx_rows = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_deliveries_%'"
        ).fetchall()
        names = {r["name"] for r in idx_rows}
        assert "ix_deliveries_post" in names
        assert "ix_deliveries_channel" in names
        assert "ix_deliveries_delivered" in names

    def test_upgrade_v6_db_with_posted_rows(self, tmp_path):
        """A v6 DB with posted rows should upgrade to v7 and backfill deliveries."""
        db_path = tmp_path / "test.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE schema_version(
              version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT
            );
            INSERT INTO schema_version VALUES
              (1, 'Initial schema', '2026-01-01T00:00:00+00:00'),
              (2, 'Drop unused tables', '2026-01-01T00:00:00+00:00'),
              (3, 'Score columns', '2026-01-01T00:00:00+00:00'),
              (4, 'Raw-story store', '2026-01-01T00:00:00+00:00'),
              (5, 'message_id', '2026-01-01T00:00:00+00:00'),
              (6, 'origin_topic', '2026-01-01T00:00:00+00:00');

            CREATE TABLE pending_posts(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL, body TEXT NOT NULL,
              category TEXT, importance INTEGER, url TEXT,
              created_at TEXT NOT NULL, posted_at TEXT,
              merge_count INTEGER NOT NULL DEFAULT 1,
              merged_urls TEXT, snippet TEXT, source_name TEXT,
              raw_json TEXT, styled_at TEXT, message_id INTEGER,
              origin_topic TEXT,
              source TEXT, published_at TEXT, upvotes INTEGER,
              comments INTEGER, stars INTEGER, reposts INTEGER,
              crosspost_count INTEGER, penalty REAL, lookback_hours REAL,
              score_at_queue REAL, engagement_score REAL,
              recency_at_queue REAL, source_weight REAL,
              topic_bonus REAL, crosspost_bonus REAL,
              matched_topics TEXT, scored_at TEXT
            );
            """
        )
        # Insert 3 posted rows + 2 unposted rows.
        posted_ts = "2026-08-28T10:00:00+00:00"
        conn.executemany(
            "INSERT INTO pending_posts(title, body, url, created_at, posted_at, message_id) "
            "VALUES(?,?,?,?,?,?)",
            [
                ("Posted1", "B", "http://1", "2026-08-28T09:00:00+00:00", posted_ts, 111),
                ("Posted2", "B", "http://2", "2026-08-28T09:00:00+00:00", posted_ts, 222),
                ("Posted3", "B", "http://3", "2026-08-28T09:00:00+00:00", posted_ts, None),
            ],
        )
        conn.executemany(
            "INSERT INTO pending_posts(title, body, url, created_at) VALUES(?,?,?,?)",
            [
                ("Unposted1", "B", "http://4", "2026-08-28T09:00:00+00:00"),
                ("Unposted2", "B", "http://5", "2026-08-28T09:00:00+00:00"),
            ],
        )
        conn.commit()
        conn.close()

        # Reopen — migration 7 should apply.
        store2 = NewsStore(db_path)
        version_row = store2._conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        assert version_row["v"] == 7

        # Backfill: 3 posted rows should have 'telegram' deliveries.
        del_count = store2._conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE channel='telegram'"
        ).fetchone()
        assert del_count["n"] == 3
        store2.close()


# --- AC 2: Backfilled row count = COUNT(*) WHERE posted_at IS NOT NULL ---


class TestBackfillCount:
    """Backfilled delivery rows must equal the count of posted pending_posts."""

    def test_backfill_count_matches_posted_rows(self, tmp_path):
        """On a v6 DB with N posted rows, deliveries should have N backfilled rows."""
        db_path = tmp_path / "test.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE schema_version(
              version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT
            );
            INSERT INTO schema_version VALUES
              (1, 'Initial', '2026-01-01T00:00:00+00:00'),
              (2, 'Drop', '2026-01-01T00:00:00+00:00'),
              (3, 'Score', '2026-01-01T00:00:00+00:00'),
              (4, 'Store', '2026-01-01T00:00:00+00:00'),
              (5, 'msg_id', '2026-01-01T00:00:00+00:00'),
              (6, 'origin', '2026-01-01T00:00:00+00:00');

            CREATE TABLE pending_posts(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL, body TEXT NOT NULL,
              category TEXT, importance INTEGER, url TEXT,
              created_at TEXT NOT NULL, posted_at TEXT,
              merge_count INTEGER NOT NULL DEFAULT 1,
              merged_urls TEXT, snippet TEXT, source_name TEXT,
              raw_json TEXT, styled_at TEXT, message_id INTEGER,
              origin_topic TEXT,
              source TEXT, published_at TEXT, upvotes INTEGER,
              comments INTEGER, stars INTEGER, reposts INTEGER,
              crosspost_count INTEGER, penalty REAL, lookback_hours REAL,
              score_at_queue REAL, engagement_score REAL,
              recency_at_queue REAL, source_weight REAL,
              topic_bonus REAL, crosspost_bonus REAL,
              matched_topics TEXT, scored_at TEXT
            );
            """
        )
        posted_ts = "2026-08-28T12:00:00+00:00"
        for i in range(5):
            conn.execute(
                "INSERT INTO pending_posts(title, body, url, created_at, posted_at, message_id) "
                "VALUES(?,?,?,?,?,?)",
                (f"Posted{i}", "B", f"http://{i}", "2026-08-28T10:00:00+00:00", posted_ts, 100 + i),
            )
        # Also insert 3 unposted rows to verify they are NOT backfilled.
        for i in range(3):
            conn.execute(
                "INSERT INTO pending_posts(title, body, url, created_at) VALUES(?,?,?,?)",
                (f"Unposted{i}", "B", f"http://u{i}", "2026-08-28T10:00:00+00:00"),
            )
        conn.commit()
        conn.close()

        store2 = NewsStore(db_path)
        posted_count = store2._conn.execute(
            "SELECT COUNT(*) AS n FROM pending_posts WHERE posted_at IS NOT NULL"
        ).fetchone()["n"]
        del_count = store2._conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE channel='telegram'"
        ).fetchone()["n"]
        assert del_count == posted_count
        assert del_count == 5
        store2.close()


# --- AC 3: mark_delivered idempotency ---


class TestMarkDeliveredIdempotent:
    """mark_delivered twice for the same (post_id, channel) leaves one row."""

    def test_mark_delivered_twice_one_row(self, store):
        """Double-deliver to the same channel should not duplicate or raise."""
        store.add_pending_post({"title": "Test", "body": "B", "url": "http://example.com"})
        post_id = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]

        # First delivery — should insert one row.
        store.mark_delivered(post_id, "telegram", message_id=42)
        rows = store._conn.execute(
            "SELECT * FROM deliveries WHERE post_id=? AND channel=?", (post_id, "telegram")
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["message_id"] == 42

        # Second delivery — should be a silent no-op, no raise.
        store.mark_delivered(post_id, "telegram", message_id=999)
        rows = store._conn.execute(
            "SELECT * FROM deliveries WHERE post_id=? AND channel=?", (post_id, "telegram")
        ).fetchall()
        assert len(rows) == 1

    def test_mark_delivered_different_channels(self, store):
        """Delivering to two different channels should produce two rows."""
        store.add_pending_post({"title": "Test", "body": "B", "url": "http://example.com"})
        post_id = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]

        store.mark_delivered(post_id, "telegram", message_id=42)
        store.mark_delivered(post_id, "girllm:gaming")
        rows = store._conn.execute(
            "SELECT * FROM deliveries WHERE post_id=? ORDER BY channel", (post_id,)
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["channel"] == "girllm:gaming"
        assert rows[1]["channel"] == "telegram"


# --- AC 4: mark_posted dual-write ---


class TestMarkPostedDualWrite:
    """mark_posted still sets posted_at and creates the telegram delivery."""

    def test_mark_posted_sets_posted_at_and_creates_delivery(self, store):
        """mark_posted should set posted_at AND insert a telegram delivery."""
        store.add_pending_post({"title": "Test", "body": "B", "url": "http://example.com"})
        post_id = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]

        store.mark_posted(post_id, message_id=77)

        # posted_at should be set.
        pp_row = store._conn.execute(
            "SELECT posted_at, message_id FROM pending_posts WHERE id=?", (post_id,)
        ).fetchone()
        assert pp_row["posted_at"] is not None
        assert pp_row["message_id"] == 77

        # telegram delivery should exist.
        del_row = store._conn.execute(
            "SELECT * FROM deliveries WHERE post_id=? AND channel='telegram'", (post_id,)
        ).fetchone()
        assert del_row is not None
        assert del_row["message_id"] == 77

    def test_mark_posted_without_message_id(self, store):
        """mark_posted without message_id should still dual-write."""
        store.add_pending_post({"title": "Test", "body": "B", "url": "http://example.com"})
        post_id = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]

        store.mark_posted(post_id)

        pp_row = store._conn.execute(
            "SELECT posted_at, message_id FROM pending_posts WHERE id=?", (post_id,)
        ).fetchone()
        assert pp_row["posted_at"] is not None

        del_row = store._conn.execute(
            "SELECT * FROM deliveries WHERE post_id=? AND channel='telegram'", (post_id,)
        ).fetchone()
        assert del_row is not None
        assert del_row["message_id"] is None

    def test_mark_posted_idempotent_delivery(self, store):
        """Calling mark_posted twice should not create duplicate deliveries."""
        store.add_pending_post({"title": "Test", "body": "B", "url": "http://example.com"})
        post_id = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]

        store.mark_posted(post_id, message_id=100)
        store.mark_posted(post_id, message_id=200)

        del_rows = store._conn.execute(
            "SELECT * FROM deliveries WHERE post_id=? AND channel='telegram'", (post_id,)
        ).fetchall()
        assert len(del_rows) == 1
