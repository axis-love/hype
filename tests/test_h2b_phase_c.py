"""Tests for flow_001162 Phase C: items 9-11.

AC coverage:
  - _pick_snapshot uses select_for_consumer with the telegram profile;
    /scores threshold equals the poster's for the same store state.
  - consumer_profile(cfg, 'nope') raises ValueError with the name in
    the message.
  - PickResult.excluded_ids exposes the cooldown exclusion set (item 10
    dedupe — jobs.py reads it instead of re-counting).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from newsbot.config import consumer_profile, _consumer_profiles
from newsbot.db import NewsStore
from newsbot.main import _pick_snapshot
from newsbot.selection import select_for_consumer


NOW = datetime.now(timezone.utc)  # real wall clock: _pick_snapshot uses real now, so rows must be fresh
CFG = {"lookback_hours": 48}


def _story(row_id: int, temp: float, *, topic: str | None = "gaming") -> dict:
    """Store-insertable story whose persisted score_breakdown yields ≈ temp.

    Mirrors tests.helpers.scored_story (real wall-clock published_at so the
    temperature stays fresh) plus an origin_topic for cooldown testing.
    """
    published = NOW.isoformat(timespec="seconds")
    return {
        "title": f"story {row_id}",
        "url": f"https://example.com/{row_id}",
        "source": "hn",
        "source_name": "Hacker News",
        "snippet": f"snip {row_id}",
        "upvotes": int(temp),
        "comments": 1,
        "published_at": published,
        "score_breakdown": {
            "source": "hn",
            "published_at": published,
            "upvotes": int(temp),
            "comments": 1,
            "stars": 0,
            "reposts": 0,
            "crosspost_count": 1,
            "penalty": 1.0,
            "lookback_hours": 48,
            "score": temp,
            "engagement": temp,
            "recency": 1.0,
            "source_weight": 1.0,
            "topic_bonus": 0,
            "crosspost_bonus": 0.0,
            "matched_topics": [topic] if topic else [],
            "origin_topic": topic,
            "scored_at": published,
        },
    }


def _pick_row(row_id: int, temp: float, *, topic: str | None = "gaming") -> dict:
    """Direct-select_for_consumer row (no store round-trip): fresh temps."""
    return {
        "id": row_id,
        "title": f"story {row_id}",
        "published_at": NOW.isoformat(),
        "engagement_score": temp,
        "source_weight": 1.0,
        "topic_bonus": 0,
        "crosspost_bonus": 0.0,
        "penalty": 1.0,
        "lookback_hours": 48.0,
        "source": "hn",
        "origin_topic": topic,
        "matched_topics": [topic] if topic else [],
        "merge_count": 1,
        "url": f"https://example.com/{row_id}",
        "snippet": "snip",
        "source_name": "hn",
        "raw_json": "{}",
        "category": "AI",
        "upvotes": int(temp),
        "comments": 1,
        "stars": 0,
        "reposts": 0,
        "crosspost_count": 1,
        "score_at_queue": temp,
        "recency_at_queue": 1.0,
        "scored_at": NOW.isoformat(),
        "styled_at": None,
        "message_id": None,
        "merged_urls": None,
    }


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    s = NewsStore(tmp_path / "h2bc.sqlite")
    yield s
    s.close()


# --- AC: consumer_profile raises ValueError with name in message ----------


class TestConsumerProfileLookup:
    """Item 11: consumer_profile(cfg, name) — single loud lookup."""

    def test_unknown_consumer_raises_valueerror_with_name(self):
        cfg = {"consumers": _consumer_profiles()}
        with pytest.raises(ValueError, match="nope"):
            consumer_profile(cfg, "nope")

    def test_error_message_includes_exact_name(self):
        cfg = {"consumers": _consumer_profiles()}
        with pytest.raises(ValueError, match="unknown consumer: nope"):
            consumer_profile(cfg, "nope")

    def test_known_consumer_returns_profile(self):
        cfg = {"consumers": _consumer_profiles()}
        profile = consumer_profile(cfg, "girllm")
        assert profile["channel"] == "girllm"

    def test_missing_consumers_key_raises(self):
        with pytest.raises(ValueError, match="unknown consumer: telegram"):
            consumer_profile({}, "telegram")


# --- AC: _pick_snapshot routes through select_for_consumer ----------------


class TestPickSnapshotParity:
    """Item 9: /scores threshold equals the poster's for the same store
    state — one source of truth."""

    def test_snapshot_threshold_matches_select_for_consumer(self, store):
        """_pick_snapshot's threshold equals select_for_consumer's for the
        same store state (telegram profile, same 24h cooldown window)."""
        store.add_stories_to_store([
            _story(1, 90.0),
            _story(2, 150.0),
            _story(3, 60.0),
        ], [])

        cfg = {"lookback_hours": 48, "consumers": _consumer_profiles()}
        result, floor, ratio, merge_bonus, merge_cap = _pick_snapshot(store, cfg)

        # Independent computation through select_for_consumer.
        profile = consumer_profile(cfg, "telegram")
        rows = store.list_store_rows("telegram")
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        deliveries = store.list_posted_since("telegram", since)
        expected = select_for_consumer(rows, deliveries, profile, cfg, now=now)

        assert result.threshold == pytest.approx(expected.threshold)
        assert result.median == pytest.approx(expected.median)
        assert result.reason == expected.reason
        assert floor == profile["floor"]
        assert ratio == profile["ratio"]

    def test_snapshot_respects_cooldown_like_poster(self, store):
        """A topic at cooldown in deliveries excludes undelivered rows of
        that topic in the snapshot exactly as the poster excludes them
        (item 9 parity under cooldown).

        Cooldown counts DISTINCT delivered posts per topic (UNIQUE(post_id,
        channel) — one delivery per post per channel), so we deliver
        cd_max separate gaming posts to hit the limit.
        """
        stories = []
        for i in range(cd_max := 3):  # default telegram cooldown_max
            stories.append(_story(i + 1, 90.0 - i, topic="gaming"))  # delivered -> feeds cooldown
        stories.append(_story(10, 110.0, topic="gaming"))  # undelivered -> excluded by cooldown
        stories.append(_story(11, 150.0, topic="ai"))        # untouched topic
        store.add_stories_to_store(stories, [])

        cfg = {"lookback_hours": 48, "consumers": _consumer_profiles()}
        profile = consumer_profile(cfg, "telegram")
        cd_max = profile["cooldown_max"]
        assert cd_max == 3

        # Deliver cd_max distinct gaming posts to reach the cooldown limit.
        for i in range(cd_max):
            rid = store._conn.execute(
                "SELECT id FROM pending_posts WHERE title=?", (f"story {i + 1}",)
            ).fetchone()["id"]
            store.mark_delivered(rid, "telegram")
        row2_id = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title=?", ("story 10",)
        ).fetchone()["id"]

        result, *_ = _pick_snapshot(store, cfg)
        # The undelivered gaming row must be excluded from the eligible set
        # (delivered rows are gone from the store view entirely).
        assert row2_id in result.excluded_ids
        # The poster's direct call excludes it too — same source of truth.
        rows = store.list_store_rows("telegram")
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        deliveries = store.list_posted_since("telegram", since)
        expected = select_for_consumer(rows, deliveries, profile, cfg, now=now)
        assert result.excluded_ids == expected.excluded_ids
        assert row2_id in expected.excluded_ids


# --- Item 10: PickResult.excluded_ids -------------------------------------


class TestPickResultExcludedIds:
    """Item 10: select_for_consumer exposes excluded ids via PickResult so
    jobs.py no longer re-implements the cooldown counting loop."""

    def test_excluded_ids_empty_without_deliveries(self):
        profile = _consumer_profiles()["telegram"]
        rows = [_pick_row(1, 90.0)]
        result = select_for_consumer(rows, [], profile, CFG, now=NOW)
        assert result.excluded_ids == frozenset()

    def test_excluded_ids_populated_on_cooldown(self):
        profile = _consumer_profiles()["telegram"]
        rows = [_pick_row(1, 90.0, topic="gaming"), _pick_row(2, 150.0, topic="ai")]
        cd_max = profile["cooldown_max"]
        deliveries = [{"origin_topic": "gaming"} for _ in range(cd_max)]
        result = select_for_consumer(rows, deliveries, profile, CFG, now=NOW)
        assert 1 in result.excluded_ids
        assert 2 not in result.excluded_ids
