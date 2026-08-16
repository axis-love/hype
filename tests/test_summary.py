"""Tests for the daily 13:00 summary job (Task 8).

Covers _run_summary (content/delivery) and _scheduler_summary_iteration
(day-key bookkeeping), both with injected ``now``.
"""
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from newsbot.db import NewsStore
from newsbot.jobs import JobCoordinator
from newsbot.main import _run_summary, _scheduler_summary_iteration

TZ = ZoneInfo("Asia/Bangkok")
NOW = datetime(2026, 8, 16, 13, 5, tzinfo=TZ)  # just past 13:00
DAY = "2026-08-16"


class MockSettings:
    def __init__(self):
        self._data: dict[str, dict[str, object]] = {}

    def get(self, section, key, default=None):
        return self._data.get(section, {}).get(key, default)

    def set(self, section, key, value):
        self._data.setdefault(section, {})[key] = value


@pytest.fixture
def store(tmp_path: Path) -> NewsStore:
    return NewsStore(tmp_path / "test.sqlite")


@pytest.fixture
def settings():
    return MockSettings()


def _seed_posted_row(store: NewsStore, title: str = "Posted story") -> None:
    """Add a store row and mark it posted just now (within the 24h window)."""
    from tests.helpers import scored_story

    store.add_stories_to_store([scored_story(title, 80.0)], [])
    row = store.list_store_rows()[0]
    store.set_styled_content(int(row["id"]), title, "Styled body text.")
    store.mark_posted(int(row["id"]))


class TestRunSummary:
    @pytest.mark.asyncio
    async def test_zero_posted_rows_skips(self, store, settings):
        """Nothing posted in 24h → skip (3), no LLM call, no delivery."""
        with patch("newsbot.main.llm_daily_summary", new_callable=AsyncMock) as mock_llm:
            result = await _run_summary(store, settings, NOW)

        assert result == 3
        assert not mock_llm.called

    @pytest.mark.asyncio
    async def test_llm_mock_posts_and_records(self, store, settings):
        """With posted rows: LLM is called, message delivered, day recorded."""
        _seed_posted_row(store, "Big launch today")

        captured: dict[str, str] = {}

        async def fake_post_digest(message, **kwargs):
            captured["message"] = message

        with patch("newsbot.main.llm_daily_summary", new_callable=AsyncMock,
                   return_value={"title": "Daily recap", "body": "Four sentences. " * 4}), \
             patch("newsbot.main.post_digest", side_effect=fake_post_digest), \
             patch("newsbot.main._build_lm_client", return_value=object()), \
             patch.dict("os.environ", {"BOT_TOKEN": "fake", "NEWS_CHANNEL_ID": "@chan"}):
            result = await _run_summary(store, settings, NOW)

        assert result == 0
        assert "Daily recap" in captured["message"]
        recorded = store.get_summary_for_day(DAY)
        assert recorded is not None
        assert recorded["item_count"] == 1

    @pytest.mark.asyncio
    async def test_llm_failure_returns_1(self, store, settings):
        """LLM returning nothing → failure (1), day not recorded."""
        _seed_posted_row(store)

        with patch("newsbot.main.llm_daily_summary", new_callable=AsyncMock, return_value=None):
            result = await _run_summary(store, settings, NOW)

        assert result == 1
        assert store.get_summary_for_day(DAY) is None

    @pytest.mark.asyncio
    async def test_delivery_failure_returns_1(self, store, settings):
        """Delivery error → failure (1), nothing recorded."""
        _seed_posted_row(store)

        async def exploding_post(message, **kwargs):
            raise RuntimeError("Telegram down")

        with patch("newsbot.main.llm_daily_summary", new_callable=AsyncMock,
                   return_value={"title": "T", "body": "B"}), \
             patch("newsbot.main.post_digest", side_effect=exploding_post), \
             patch("newsbot.main._build_lm_client", return_value=object()), \
             patch.dict("os.environ", {"BOT_TOKEN": "fake", "NEWS_CHANNEL_ID": "@chan"}):
            result = await _run_summary(store, settings, NOW)

        assert result == 1
        assert store.get_summary_for_day(DAY) is None

    @pytest.mark.asyncio
    async def test_unique_day_guard_tolerates_rerecord(self, store, settings):
        """Recording the same day twice (re-delivery race) is not an error."""
        _seed_posted_row(store)
        store.add_summary(DAY, "first", "model", 1)

        with patch("newsbot.main.llm_daily_summary", new_callable=AsyncMock,
                   return_value={"title": "T", "body": "B"}), \
             patch("newsbot.main.post_digest", new_callable=AsyncMock), \
             patch("newsbot.main._build_lm_client", return_value=object()), \
             patch.dict("os.environ", {"BOT_TOKEN": "fake", "NEWS_CHANNEL_ID": "@chan"}):
            result = await _run_summary(store, settings, NOW)

        assert result == 0  # UNIQUE violation tolerated
        assert store.get_summary_for_day(DAY)["summary_text"] == "first"


class TestSchedulerSummaryIteration:
    @pytest.mark.asyncio
    async def test_before_13_is_idle(self, store, settings):
        coordinator = JobCoordinator(store, settings)

        result = await _scheduler_summary_iteration(
            coordinator, store, settings, now=NOW.replace(hour=12, minute=59),
        )

        assert result == 0
        assert settings.get("scheduler", "last_summary_day", default="") == ""

    @pytest.mark.asyncio
    async def test_success_consumes_day(self, store, settings):
        """After 13:00 with posted rows, the day key is written."""
        coordinator = JobCoordinator(store, settings)
        _seed_posted_row(store)

        with patch("newsbot.main.llm_daily_summary", new_callable=AsyncMock,
                   return_value={"title": "T", "body": "B"}), \
             patch("newsbot.main.post_digest", new_callable=AsyncMock), \
             patch("newsbot.main._build_lm_client", return_value=object()), \
             patch.dict("os.environ", {"BOT_TOKEN": "fake", "NEWS_CHANNEL_ID": "@chan"}):
            result = await _scheduler_summary_iteration(coordinator, store, settings, now=NOW)

        assert result == 0
        assert settings.get("scheduler", "last_summary_day") == DAY

    @pytest.mark.asyncio
    async def test_empty_day_consumes_day(self, store, settings):
        """Nothing posted → skip (3) still consumes the day."""
        coordinator = JobCoordinator(store, settings)

        result = await _scheduler_summary_iteration(coordinator, store, settings, now=NOW)

        assert result == 3
        assert settings.get("scheduler", "last_summary_day") == DAY

    @pytest.mark.asyncio
    async def test_double_run_same_day_noop(self, store, settings):
        """Second run for the same day is a no-op (no second LLM call)."""
        coordinator = JobCoordinator(store, settings)
        _seed_posted_row(store)

        with patch("newsbot.main.llm_daily_summary", new_callable=AsyncMock,
                   return_value={"title": "T", "body": "B"}) as mock_llm, \
             patch("newsbot.main.post_digest", new_callable=AsyncMock), \
             patch("newsbot.main._build_lm_client", return_value=object()), \
             patch.dict("os.environ", {"BOT_TOKEN": "fake", "NEWS_CHANNEL_ID": "@chan"}):
            result1 = await _scheduler_summary_iteration(coordinator, store, settings, now=NOW)
            result2 = await _scheduler_summary_iteration(coordinator, store, settings, now=NOW)

        assert result1 == 0 and result2 == 0
        assert mock_llm.call_count == 1

    @pytest.mark.asyncio
    async def test_failure_leaves_day_unconsumed(self, store, settings):
        """LLM failure leaves the day unconsumed — retries on next tick."""
        coordinator = JobCoordinator(store, settings)
        _seed_posted_row(store)

        with patch("newsbot.main.llm_daily_summary", new_callable=AsyncMock, return_value=None):
            result = await _scheduler_summary_iteration(coordinator, store, settings, now=NOW)

        assert result == 1
        assert settings.get("scheduler", "last_summary_day", default="") == ""
