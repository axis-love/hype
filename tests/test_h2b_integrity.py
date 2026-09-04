"""Tests for flow_001162 Phase A: deliveries integrity (items 1-3).

1. prune_delivered leaves zero orphan deliveries.
2. mark_delivered rejects nonexistent post_id (raises).
3. mark_posted is atomic: injected failure on delivery insert leaves
   posted_at NULL (transaction rolled back).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from newsbot.db import NewsStore


def _iso(days_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
        timespec="seconds"
    )


def _bd(**overrides) -> dict:
    base = {
        "score": 100.0, "engagement": 80.0, "recency": 0.9,
        "source_weight": 1.0, "topic_bonus": 0, "crosspost_bonus": 0.0,
        "penalty": 1.0, "matched_topics": [], "origin_topic": "gaming",
        "scored_at": _iso(0), "lookback_hours": 48.0, "source": "reddit",
        "published_at": _iso(0), "upvotes": 100, "comments": 10,
        "stars": 0, "reposts": 0, "crosspost_count": 1,
    }
    base.update(overrides)
    return base


def _story(title="Story A", url="https://a.example.com/1") -> dict:
    return {
        "title": title, "url": url, "category": "AI",
        "snippet": "A snippet.", "source_name": "Hacker News",
        "source": "hn", "raw_json": {"payload": "x"},
        "score_breakdown": _bd(),
    }


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    s = NewsStore(tmp_path / "h2b_store.sqlite")
    yield s
    s.close()


class TestPruneOrphanDeliveries:
    """Item 1: prune_delivered must also clean up orphan delivery rows."""

    def test_prune_leaves_zero_orphan_deliveries(self, store):
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]
        old_ts = _iso(60)
        store._conn.execute(
            "INSERT OR IGNORE INTO deliveries(post_id, channel, delivered_at, message_id) "
            "VALUES(?,?,?,?)",
            (rid, "telegram", old_ts, None),
        )

        deleted = store.prune_delivered(max_age_days=30)
        assert deleted == 1

        orphans = store._conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE post_id NOT IN "
            "(SELECT id FROM pending_posts)"
        ).fetchone()
        assert orphans["n"] == 0, "prune_delivered must not leave orphan deliveries"

    def test_prune_multiple_orphans_cleaned(self, store):
        old_ts = _iso(365)
        for i in range(5):
            store.add_stories_to_store(
                [_story(title=f"S{i}", url=f"https://s{i}.example.com")], []
            )
            rid = store._conn.execute(
                "SELECT id FROM pending_posts WHERE title=?", (f"S{i}",)
            ).fetchone()["id"]
            store._conn.execute(
                "INSERT OR IGNORE INTO deliveries(post_id, channel, delivered_at, message_id) "
                "VALUES(?,?,?,?)",
                (rid, "telegram", old_ts, None),
            )

        deleted = store.prune_delivered(max_age_days=30, batch_size=10)
        assert deleted == 5
        orphans = store._conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE post_id NOT IN "
            "(SELECT id FROM pending_posts)"
        ).fetchone()
        assert orphans["n"] == 0


class TestMarkDeliveredExistenceCheck:
    """Item 2: mark_delivered rejects nonexistent post_id."""

    def test_nonexistent_post_raises(self, store):
        with pytest.raises(ValueError, match="post_id"):
            store.mark_delivered(999999, "telegram")

    def test_existing_post_still_works(self, store):
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]
        store.mark_delivered(rid, "telegram")
        count = store._conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE post_id=?", (rid,)
        ).fetchone()
        assert count["n"] == 1


class TestMarkPostedAtomicity:
    """Item 3: mark_posted runs both writes inside one transaction."""

    def test_injected_failure_rolls_back_posted_at(self, store):
        """mark_posted must run both writes in one transaction. If the
        delivery INSERT fails, posted_at must be NULL (rolled back).

        We test this by replacing the connection with a wrapper that
        raises on the deliveries INSERT.
        """
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]

        # Replace the connection with a wrapper that raises on deliveries INSERT.
        original_conn = store._conn

        class FailingConn:
            def __init__(self, real):
                self._real = real

            def cursor(self):
                return _FailingCursor(self._real.cursor())

            def execute(self, sql, params=()):
                if "INSERT" in sql.upper() and "deliveries" in sql:
                    raise sqlite3.OperationalError("injected failure")
                return self._real.execute(sql, params)

            def row_factory(self, val):
                self._real.row_factory = val

            row_factory = property(
                lambda self: self._real.row_factory,
                lambda self, val: setattr(self._real, 'row_factory', val),
            )

            def close(self):
                self._real.close()

        class _FailingCursor:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, params=()):
                if "INSERT" in sql.upper() and "deliveries" in sql:
                    raise sqlite3.OperationalError("injected failure")
                return self._real.execute(sql, params)

            @property
            def rowcount(self):
                return self._real.rowcount

            def close(self):
                self._real.close()

        store._conn = FailingConn(original_conn)
        try:
            with pytest.raises(sqlite3.OperationalError):
                store.mark_posted(rid, message_id=42)
        finally:
            store._conn = original_conn

        # posted_at must NOT be set (transaction rolled back).
        row = store._conn.execute(
            "SELECT posted_at FROM pending_posts WHERE id=?", (rid,)
        ).fetchone()
        assert row["posted_at"] is None, \
            "posted_at must be NULL when delivery insert fails (atomic)"

    def test_mark_posted_success_sets_both(self, store):
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]
        store.mark_posted(rid, message_id=42)

        row = store._conn.execute(
            "SELECT posted_at, message_id FROM pending_posts WHERE id=?", (rid,)
        ).fetchone()
        assert row["posted_at"] is not None
        assert row["message_id"] == 42

        d = store._conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE post_id=? AND channel='telegram'",
            (rid,),
        ).fetchone()
        assert d["n"] == 1


class TestStoreEncapsulation:
    """is_delivered and schema_version encapsulate _conn access (H4 NIT)."""

    def test_is_delivered_false_before_delivery(self, store):
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        assert store.is_delivered(rid, "telegram") is False

    def test_is_delivered_true_after_delivery(self, store):
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        store.mark_delivered(rid, "telegram")
        assert store.is_delivered(rid, "telegram") is True

    def test_is_delivered_channel_specific(self, store):
        """Delivery to one channel does not register as delivered to another."""
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        store.mark_delivered(rid, "telegram")
        assert store.is_delivered(rid, "telegram") is True
        assert store.is_delivered(rid, "girllm") is False

    def test_schema_version_is_int(self, store):
        v = store.schema_version()
        assert isinstance(v, int)
        assert v >= 8
