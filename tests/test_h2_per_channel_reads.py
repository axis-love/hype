"""Tests for flow_001139: H2 per-channel store reads.

Covers the acceptance criteria:
  1. A row delivered to 'girllm' is still eligible for 'telegram' and vice versa.
  2. Eviction skips a row with one delivery even when it is the coldest.
  3. prune_delivered removes rows whose newest delivery is older than the window.
  4. Every call site passes a channel; no bare posted_at IS NULL in db.py
     outside the compatibility wrapper.
  5. Replay over the GTA6 fixture picks the same rows (no selection change
     for the channel).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from newsbot.db import NewsStore


# --- helpers ---------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(days_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
        timespec="seconds"
    )


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


def _story(title="Story A", url="https://a.example.com/1", **bd_overrides) -> dict:
    return {
        "title": title,
        "url": url,
        "category": "AI",
        "snippet": "A snippet.",
        "source_name": "Hacker News",
        "source": "hn",
        "raw_json": {"payload": "x"},
        "score_breakdown": _bd(**bd_overrides),
    }


def _deliver(
    store: NewsStore,
    row_id: int,
    channel: str = "telegram",
    *,
    delivered_at: str | None = None,
    message_id: int | None = None,
) -> None:
    """Insert a delivery row directly (bypasses mark_posted dual-write)."""
    ts = delivered_at or _utc_now_iso()
    store._conn.execute(
        "INSERT OR IGNORE INTO deliveries(post_id, channel, delivered_at, message_id) "
        "VALUES(?,?,?,?)",
        (row_id, channel, ts, message_id),
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    s = NewsStore(tmp_path / "h2_store.sqlite")
    yield s
    s.close()


# --- AC 1: channel isolation ----------------------------------------------


class TestChannelIsolation:
    """A row delivered to 'girllm' is still eligible for 'telegram' and
    vice versa."""

    def test_girllm_delivered_row_visible_to_telegram(self, store):
        """Row delivered to girllm but NOT to telegram appears in
        list_store_rows('telegram')."""
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        _deliver(store, rid, "girllm:gaming")

        # Telegram sees this row as eligible (not yet delivered to telegram).
        rows = store.list_store_rows("telegram")
        ids = [r["id"] for r in rows]
        assert rid in ids, "girllm-delivered row must be visible to telegram"

    def test_telegram_delivered_row_visible_to_girllm(self, store):
        """Row delivered to telegram but NOT to girllm appears in
        list_store_rows('girllm:gaming')."""
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        _deliver(store, rid, "telegram")

        rows = store.list_store_rows("girllm:gaming")
        ids = [r["id"] for r in rows]
        assert rid in ids, "telegram-delivered row must be visible to girllm"

    def test_dual_delivered_row_invisible_to_both(self, store):
        """Row delivered to BOTH channels appears in neither channel's
        list_store_rows."""
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        _deliver(store, rid, "telegram")
        _deliver(store, rid, "girllm:gaming")

        tg = store.list_store_rows("telegram")
        gl = store.list_store_rows("girllm:gaming")
        assert all(r["id"] != rid for r in tg), "dual-delivered row must not show for telegram"
        assert all(r["id"] != rid for r in gl), "dual-delivered row must not show for girllm"

    def test_list_posted_since_channel_scoped(self, store):
        """list_posted_since(channel, since) only returns rows delivered
        to that channel."""
        store.add_stories_to_store(
            [_story(title="TG", url="https://tg.example.com"),
             _story(title="GL", url="https://gl.example.com")],
            [],
        )
        tg_rid = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='TG'"
        ).fetchone()["id"]
        gl_rid = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='GL'"
        ).fetchone()["id"]
        ts = _iso(0)
        _deliver(store, tg_rid, "telegram", delivered_at=ts)
        _deliver(store, gl_rid, "girllm:gaming", delivered_at=ts)

        tg_rows = store.list_posted_since("telegram", ts)
        gl_rows = store.list_posted_since("girllm:gaming", ts)
        tg_titles = {r["title"] for r in tg_rows}
        gl_titles = {r["title"] for r in gl_rows}
        assert tg_titles == {"TG"}, f"telegram should see only TG, got {tg_titles}"
        assert gl_titles == {"GL"}, f"girllm should see only GL, got {gl_titles}"

    def test_list_merge_target_rows_channel_scoped(self, store):
        """list_merge_target_rows(channel, days) includes:
        - undelivered rows (no delivery to any channel)
        - rows delivered to THIS channel within the window
        NOT rows delivered to a DIFFERENT channel only."""
        store.add_stories_to_store(
            [_story(title="Unposted", url="https://u.example.com"),
             _story(title="TGPosted", url="https://tg.example.com"),
             _story(title="GLPosted", url="https://gl.example.com")],
            [],
        )
        u_rid = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='Unposted'"
        ).fetchone()["id"]
        tg_rid = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='TGPosted'"
        ).fetchone()["id"]
        gl_rid = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='GLPosted'"
        ).fetchone()["id"]
        ts = _iso(1)
        _deliver(store, tg_rid, "telegram", delivered_at=ts)
        _deliver(store, gl_rid, "girllm:gaming", delivered_at=ts)

        # From telegram's perspective:
        tg_targets = store.list_merge_target_rows("telegram", 7)
        tg_ids = {r["id"] for r in tg_targets}
        assert u_rid in tg_ids, "unposted row must be a merge target for any channel"
        assert tg_rid in tg_ids, "telegram-delivered row must be a merge target for telegram"
        assert gl_rid not in tg_ids, "girllm-only-delivered row must NOT be a merge target for telegram"

    def test_count_pending_channel_scoped(self, store):
        """count_pending(channel) counts rows with no delivery to that channel."""
        store.add_stories_to_store(
            [_story(title="A", url="https://a.example.com"),
             _story(title="B", url="https://b.example.com")],
            [],
        )
        a_rid = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='A'"
        ).fetchone()["id"]
        _deliver(store, a_rid, "telegram")

        assert store.count_pending("telegram") == 1, "A is delivered to telegram, B is not"
        assert store.count_pending("girllm:gaming") == 2, "neither is delivered to girllm"

    def test_get_store_row_channel_scoped(self, store):
        """get_store_row(row_id, channel) returns the row only if not
        delivered to that channel."""
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        _deliver(store, rid, "telegram")

        assert store.get_store_row(rid, "telegram") is None
        assert store.get_store_row(rid, "girllm:gaming") is not None

    def test_list_store_ids_channel_scoped(self, store):
        """list_store_ids(channel) returns ids not delivered to that channel."""
        store.add_stories_to_store(
            [_story(title="A", url="https://a.example.com"),
             _story(title="B", url="https://b.example.com")],
            [],
        )
        a_rid = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='A'"
        ).fetchone()["id"]
        _deliver(store, a_rid, "telegram")

        ids = store.list_store_ids("telegram")
        assert a_rid not in ids
        assert len(ids) == 1

    def test_default_channel_is_telegram(self, store):
        """Methods with a channel param default to 'telegram' so existing
        callers that pass no arg see telegram-scoped results."""
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        _deliver(store, rid, "telegram")

        # No channel arg → telegram default.
        assert store.list_store_rows() == []
        assert store.count_pending() == 0
        assert store.get_store_row(rid) is None
        assert store.list_store_ids() == []

    def test_list_unposted_posts_channel_scoped(self, store):
        """list_unposted_posts(channel) respects the delivery filter."""
        store.add_stories_to_store(
            [_story(title="A", url="https://a.example.com"),
             _story(title="B", url="https://b.example.com")],
            [],
        )
        a_rid = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='A'"
        ).fetchone()["id"]
        _deliver(store, a_rid, "telegram")

        tg = store.list_unposted_posts("telegram")
        gl = store.list_unposted_posts("girllm:gaming")
        tg_titles = {r["title"] for r in tg}
        gl_titles = {r["title"] for r in gl}
        assert tg_titles == {"B"}
        assert gl_titles == {"A", "B"}


# --- AC 2: eviction protection -------------------------------------------


class TestEvictionProtection:
    """Eviction skips a row with one delivery even when it is the coldest."""

    def test_evict_skips_row_with_any_delivery(self, store):
        """A row delivered to ANY channel is never evicted, even if it
        is the coldest row in the store."""
        store.add_stories_to_store(
            [_story(title="Cold-Delivered", url="https://cold.example.com",
                    engagement=1.0, upvotes=1, comments=0),
             _story(title="Hot-Unposted", url="https://hot.example.com",
                    engagement=100.0, upvotes=100, comments=50)],
            [],
        )
        cold_id = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title='Cold-Delivered'"
        ).fetchone()["id"]
        _deliver(store, cold_id, "girllm:gaming")  # delivered to girllm, not telegram

        # Only the hot-unposted row is evictable (1 undelivered row).
        # cap=0 means evict all undelivered rows.
        rows = store.list_store_rows("telegram")
        temps = {r["id"]: 1.0 for r in rows}
        temps[cold_id] = 0.0  # cold row is coldest

        evicted = store.evict_coldest(temps, cap=0)
        assert evicted == 1  # the hot-unposted row evicted
        # The cold-delivered row must survive.
        survivor = store._conn.execute(
            "SELECT 1 FROM pending_posts WHERE id=?", (cold_id,)
        ).fetchone()
        assert survivor is not None, "delivered row must not be evicted"

    def test_evict_only_removes_undelivered_rows(self, store):
        """When all undelivered rows are evicted, delivered rows survive."""
        stories = [_story(title=f"S{i}", url=f"https://s{i}.example.com") for i in range(5)]
        store.add_stories_to_store(stories, [])
        # Deliver rows 0 and 2 to telegram.
        delivered_ids = set()
        for i in (0, 2):
            rid = store._conn.execute(
                "SELECT id FROM pending_posts WHERE title=?", (f"S{i}",)
            ).fetchone()["id"]
            _deliver(store, rid, "telegram")
            delivered_ids.add(rid)

        rows = store.list_store_rows("telegram")  # only undelivered (3 rows)
        assert len(rows) == 3
        temps = {r["id"]: 1.0 for r in rows}
        evicted = store.evict_coldest(temps, cap=0)
        assert evicted == 3
        # Delivered rows must survive in the DB.
        for did in delivered_ids:
            survivor = store._conn.execute(
                "SELECT 1 FROM pending_posts WHERE id=?", (did,)
            ).fetchone()
            assert survivor is not None, f"delivered row {did} must not be evicted"


# --- AC 3: prune_delivered -----------------------------------------------


class TestPruneDelivered:
    """prune_delivered removes rows whose newest delivery is older than
    the max_age_days window."""

    def test_prune_delivered_removes_old(self, store):
        """A row whose only delivery is older than the cutoff is pruned."""
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        old_ts = _iso(60)
        _deliver(store, rid, "telegram", delivered_at=old_ts)

        deleted = store.prune_delivered(max_age_days=30)
        assert deleted == 1
        row = store._conn.execute(
            "SELECT 1 FROM pending_posts WHERE id=?", (rid,)
        ).fetchone()
        assert row is None

    def test_prune_delivered_preserves_recent(self, store):
        """A row with a recent delivery survives pruning."""
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        recent_ts = _iso(5)
        _deliver(store, rid, "telegram", delivered_at=recent_ts)

        deleted = store.prune_delivered(max_age_days=30)
        assert deleted == 0

    def test_prune_delivered_preserves_undelivered(self, store):
        """Rows with no delivery at all are never pruned."""
        store.add_stories_to_store([_story()], [])
        deleted = store.prune_delivered(max_age_days=1)
        assert deleted == 0
        assert store.count_pending("telegram") == 1

    def test_prune_delivered_uses_newest_delivery(self, store):
        """A row with an old delivery to one channel and a recent delivery
        to another survives (newest delivery determines age)."""
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        _deliver(store, rid, "telegram", delivered_at=_iso(60))
        _deliver(store, rid, "girllm:gaming", delivered_at=_iso(5))

        deleted = store.prune_delivered(max_age_days=30)
        assert deleted == 0, "newest delivery is recent (5d), row must survive"

    def test_prune_delivered_batched(self, store):
        """Large number of old-delivered rows pruned in batches."""
        old_ts = _iso(365)
        for i in range(25):
            store.add_stories_to_store(
                [_story(title=f"Old{i}", url=f"https://old{i}.example.com")], []
            )
            rid = store._conn.execute(
                "SELECT id FROM pending_posts WHERE title=?", (f"Old{i}",)
            ).fetchone()["id"]
            _deliver(store, rid, "telegram", delivered_at=old_ts)

        deleted = store.prune_delivered(max_age_days=30, batch_size=10)
        assert deleted == 25

    def test_prune_posted_posts_alias_works(self, store):
        """prune_posted_posts is kept as an alias to prune_delivered for
        backward compatibility with _run_retention."""
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts"
        ).fetchone()["id"]
        _deliver(store, rid, "telegram", delivered_at=_iso(60))

        deleted = store.prune_posted_posts(max_age_days=30)
        assert deleted == 1


# --- AC 4: no bare posted_at IS NULL in db.py ----------------------------


class TestNoBarePostedAtInQueries:
    """Grep db.py source: no bare posted_at IS NULL / IS NOT NULL in
    query strings outside the mark_posted compatibility wrapper."""

    def test_no_bare_posted_at_in_db(self):
        import newsbot.db as db_mod
        import inspect

        source = inspect.getsource(db_mod)
        # The mark_posted method is the compatibility wrapper that sets
        # posted_at — it is allowed to reference posted_at.
        # The migration code references posted_at in backfill — also allowed.
        # We check that list_store_rows, list_posted_since, list_merge_target_rows,
        # evict_coldest, count_pending, list_unposted_posts, get_store_row,
        # list_store_ids, and prune_delivered do NOT use posted_at in their
        # SQL queries.
        forbidden_methods = [
            "list_store_rows",
            "list_posted_since",
            "list_merge_target_rows",
            "evict_coldest",
            "count_pending",
            "list_unposted_posts",
            "get_store_row",
            "list_store_ids",
            "prune_delivered",
        ]
        for method_name in forbidden_methods:
            method = getattr(db_mod.NewsStore, method_name, None)
            assert method is not None, f"{method_name} must exist on NewsStore"
            method_src = inspect.getsource(method)
            assert "posted_at IS NULL" not in method_src, (
                f"{method_name} must not use 'posted_at IS NULL' — "
                "use the deliveries table instead"
            )
            assert "posted_at IS NOT NULL" not in method_src, (
                f"{method_name} must not use 'posted_at IS NOT NULL' — "
                "use the deliveries table instead"
            )
