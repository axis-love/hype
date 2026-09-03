"""Tests for flow_001140: H3 consumer profiles — per-consumer selection.

Covers the acceptance criteria:
  1. Telegram profile defaults equal the current env defaults.
  2. Mixed-topic fixture: girllm median computed over gaming/gamedev/ai
     rows only; a science row never appears for girllm.
  3. Consumer cooldown counts only that consumer's deliveries.
  4. Unknown consumer name raises a clear error.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from newsbot.config import _consumer_profiles
from newsbot.db import NewsStore
from newsbot.selection import select_for_consumer


# --- helpers ---------------------------------------------------------------


NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
CFG = {"lookback_hours": 48}


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
        "scored_at": NOW.isoformat(),
        "lookback_hours": 48.0,
        "source": "reddit",
        "published_at": NOW.isoformat(),
        "upvotes": 100,
        "comments": 10,
        "stars": 0,
        "reposts": 0,
        "crosspost_count": 1,
    }
    base.update(overrides)
    return base


def _row(row_id: int, temp: float, *, topic: str | None = "gaming", matched: list | None = None, merge_count: int = 1) -> dict:
    """Build a store row whose current_temperature at NOW equals `temp`."""
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
        "source": "reddit",
        "origin_topic": topic,
        "matched_topics": matched or ([topic] if topic else []),
        "merge_count": merge_count,
        "url": f"https://example.com/{row_id}",
        "snippet": "snip",
        "source_name": "r/test",
        "raw_json": "{}",
        "category": "AI",
        "upvotes": int(temp),
        "comments": 0,
        "stars": 0,
        "reposts": 0,
        "crosspost_count": 1,
        "score_at_queue": temp,
        "recency_at_queue": 0.9,
        "scored_at": NOW.isoformat(),
        "styled_at": None,
        "message_id": None,
        "merged_urls": None,
    }


def _delivery(row_id: int, topic: str, hours_ago: int = 1) -> dict:
    """A delivery row as returned by list_posted_since."""
    return {
        "id": row_id,
        "origin_topic": topic,
        "posted_at": (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
    }


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    s = NewsStore(tmp_path / "h3_store.sqlite")
    yield s
    s.close()


# --- AC 1: telegram defaults ----------------------------------------------


class TestTelegramProfileDefaults:
    """Telegram profile defaults equal the current env defaults."""

    def test_floor_default(self, monkeypatch):
        monkeypatch.delenv("NEWS_TEMP_FLOOR", raising=False)
        profiles = _consumer_profiles()
        assert profiles["telegram"]["floor"] == 35.0

    def test_ratio_default(self, monkeypatch):
        monkeypatch.delenv("NEWS_THRESHOLD_RATIO", raising=False)
        profiles = _consumer_profiles()
        assert profiles["telegram"]["ratio"] == 0.5

    def test_cooldown_default(self, monkeypatch):
        monkeypatch.delenv("NEWS_TOPIC_COOLDOWN_MAX", raising=False)
        profiles = _consumer_profiles()
        assert profiles["telegram"]["cooldown_max"] == 3

    def test_max_candidates_default(self, monkeypatch):
        monkeypatch.delenv("NEWS_MAX_CANDIDATES", raising=False)
        profiles = _consumer_profiles()
        assert profiles["telegram"]["max_candidates"] == 20

    def test_topics_none_for_telegram(self):
        profiles = _consumer_profiles()
        assert profiles["telegram"]["topics"] is None

    def test_channel_is_telegram(self):
        profiles = _consumer_profiles()
        assert profiles["telegram"]["channel"] == "telegram"


# --- AC 2: mixed-topic median, science excluded for girllm ----------------


class TestMixedTopicMedian:
    """Mixed-topic fixture (AI rows hot, gaming rows warm): girllm
    selection median is computed over gaming/gamedev/ai rows only;
    a science row never appears for girllm."""

    def test_science_row_excluded_from_girllm(self):
        """A science row with the highest temperature is invisible
        to girllm — it never appears as the pick."""
        rows = [
            # Science row — hottest, but girllm doesn't cover science.
            _row(1, 200.0, topic="science"),
            # AI row — warm, girllm covers 'ai'.
            _row(2, 100.0, topic="ai"),
            # Gaming row — warm, girllm covers 'gaming'.
            _row(3, 90.0, topic="gaming"),
        ]
        girllm_profile = _consumer_profiles()["girllm"]
        result = select_for_consumer(rows, [], girllm_profile, CFG, now=NOW)

        assert result.reason == "picked"
        assert result.row is not None
        assert result.row["id"] != 1, "science row must never appear for girllm"
        assert result.row["origin_topic"] in ("ai", "gaming"), \
            "girllm pick must be from ai/gaming/gamedev"

    def test_girllm_median_excludes_science(self):
        """The median for girllm is computed over gaming/gamedev/ai rows
        only, so a science row with 200 temp doesn't inflate it."""
        rows = [
            _row(1, 200.0, topic="science"),  # excluded by topic filter
            _row(2, 100.0, topic="ai"),
            _row(3, 90.0, topic="gaming"),
        ]
        girllm_profile = _consumer_profiles()["girllm"]
        result = select_for_consumer(rows, [], girllm_profile, CFG, now=NOW)

        # Median should be computed over [100, 90] = 95.0, NOT [200, 100, 90] = 100.0
        # With floor=25, ratio=0.3: threshold = max(25, 0.3 * 95) = max(25, 28.5) = 28.5
        # Both ai(100) and gaming(90) pass threshold. Hottest = 100 (ai).
        assert result.median == 95.0, \
            f"median should be 95.0 (gaming+ai only), got {result.median}"
        assert result.row is not None
        assert result.row["id"] == 2, "ai row (temp 100) should be picked"

    def test_telegram_includes_all_topics(self):
        """Telegram profile (topics=None) includes science rows."""
        rows = [
            _row(1, 200.0, topic="science"),
            _row(2, 100.0, topic="ai"),
        ]
        tg_profile = _consumer_profiles()["telegram"]
        result = select_for_consumer(rows, [], tg_profile, CFG, now=NOW)

        assert result.reason == "picked"
        assert result.row is not None
        assert result.row["id"] == 1, "science row (hottest) should be picked by telegram"
        # Median includes all rows.
        assert result.median == 150.0  # median of [200, 100]

    def test_matched_topics_intersection(self):
        """A row with origin_topic=None but matched_topics including
        'gaming' is visible to girllm."""
        rows = [
            _row(1, 100.0, topic=None, matched=["gaming", "other"]),
            _row(2, 50.0, topic=None, matched=["science"]),
        ]
        girllm_profile = _consumer_profiles()["girllm"]
        result = select_for_consumer(rows, [], girllm_profile, CFG, now=NOW)

        # Row 1 matches 'gaming' in matched_topics; row 2 doesn't.
        assert result.row is not None
        assert result.row["id"] == 1, "row with matched_topics=['gaming'] should be visible to girllm"


# --- AC 3: per-consumer cooldown -----------------------------------------


class TestPerConsumerCooldown:
    """Consumer cooldown counts only that consumer's deliveries."""

    def test_girllm_deliveries_dont_count_for_telegram(self):
        """3 gaming posts delivered to girllm (not telegram) — telegram's
        cooldown is NOT triggered (counts only telegram deliveries).

        select_for_consumer receives deliveries_for_channel — the caller
        scopes them to this channel. For telegram, girllm's deliveries
        are invisible (empty list)."""
        rows = [
            _row(1, 100.0, topic="gaming"),
        ]
        tg_profile = _consumer_profiles()["telegram"]
        # Telegram has NO deliveries of its own — girllm's 3 don't count.
        deliveries: list[dict] = []
        result = select_for_consumer(rows, deliveries, tg_profile, CFG, now=NOW)
        assert result.reason == "picked", \
            "telegram cooldown should not count girllm deliveries"

    def test_telegram_deliveries_dont_count_for_girllm(self):
        """3 gaming posts delivered to telegram — girllm's cooldown
        is NOT triggered (counts only girllm deliveries).

        select_for_consumer receives deliveries_for_channel — the caller
        scopes them to this channel. For girllm, telegram's deliveries
        are invisible (empty list)."""
        rows = [
            _row(1, 100.0, topic="gaming"),
        ]
        girllm_profile = _consumer_profiles()["girllm"]
        # Girllm has NO deliveries of its own — telegram's 3 don't count.
        deliveries: list[dict] = []
        result = select_for_consumer(rows, deliveries, girllm_profile, CFG, now=NOW)
        assert result.reason == "picked", \
            "girllm cooldown should not count telegram deliveries"

    def test_girllm_cooldown_triggers_on_own_deliveries(self):
        """2 gaming posts delivered to girllm + cooldown_max=2 -> 3rd
        gaming row excluded (girllm counts its own deliveries)."""
        rows = [
            _row(1, 100.0, topic="gaming"),
            _row(2, 80.0, topic="ai"),
        ]
        girllm_profile = _consumer_profiles()["girllm"]
        assert girllm_profile["cooldown_max"] == 2
        deliveries = [
            _delivery(10, "gaming", hours_ago=1),
            _delivery(11, "gaming", hours_ago=2),
        ]
        result = select_for_consumer(rows, deliveries, girllm_profile, CFG, now=NOW)
        assert result.reason == "picked"
        assert result.row is not None
        assert result.row["id"] == 2, \
            "gaming row should be excluded (2 girllm deliveries), ai row should be picked"


# --- AC 4: unknown consumer ----------------------------------------------


class TestUnknownConsumer:
    """Unknown consumer name raises a clear error."""

    @pytest.mark.asyncio
    async def test_jobs_raises_on_missing_profile(self, store, monkeypatch):
        """jobs._deliver_one raises ValueError naming the consumer when the
        telegram profile is missing from config (flow_001162 item 11:
        consumer_profile helper)."""
        from newsbot.jobs import JobCoordinator
        from core.settings_store import default_store
        from newsbot.config import load_config

        settings = default_store(str(store.db_path))
        coordinator = JobCoordinator(store, settings)

        # Patch load_config to return a config without consumers.
        bad_config = load_config(settings)
        bad_config.pop("consumers", None)
        monkeypatch.setattr("newsbot.jobs.load_config", lambda s: bad_config)

        with pytest.raises(ValueError, match="unknown consumer: telegram"):
            await coordinator._deliver_one()

    def test_select_for_consumer_with_empty_rows(self):
        """select_for_consumer with no rows returns empty result (not
        an error — the consumer profile is valid, just no data)."""
        girllm_profile = _consumer_profiles()["girllm"]
        result = select_for_consumer([], [], girllm_profile, CFG, now=NOW)
        assert result.reason == "empty"
        assert result.row is None
