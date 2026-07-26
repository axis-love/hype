"""Tests for newsbot/jobs.py — JobCoordinator serialization and drain logic."""
import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from newsbot.db import NewsStore
from newsbot.jobs import JobCoordinator, format_post_message


@pytest.fixture
def store(tmp_path: Path) -> NewsStore:
    return NewsStore(tmp_path / "test.sqlite")


@pytest.fixture
def settings():
    """Minimal mock settings store."""
    class MockSettings:
        def __init__(self):
            self._data: dict[str, dict[str, any]] = {}
        def get(self, section, key, default=None):
            return self._data.get(section, {}).get(key, default)
        def set(self, section, key, value):
            self._data.setdefault(section, {})[key] = value
    return MockSettings()


@pytest.fixture
def coordinator(store, settings) -> JobCoordinator:
    return JobCoordinator(store, settings)


class TestJobCoordinatorSerialization:
    """Verify that the coordinator serializes generation and posting."""

    @pytest.mark.asyncio
    async def test_generation_lock_prevents_overlap(self, coordinator):
        """Two concurrent generation calls — only one should run, the other skipped."""
        call_count = 0

        async def slow_gen():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.1)

        # Launch two concurrently.
        results = await asyncio.gather(
            coordinator.run_generation(slow_gen),
            coordinator.run_generation(slow_gen),
        )
        # One should run (True), one should be skipped (False).
        assert results.count(True) == 1
        assert results.count(False) == 1
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_posting_lock_prevents_overlap(self, coordinator):
        """Two concurrent posting calls — only one should run, the other skipped."""
        # Add a pending post so posting has something to do.
        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        # Mock the actual posting to be slow.
        async def slow_post(*args, **kwargs):
            await asyncio.sleep(0.1)
            return 0

        with patch.object(coordinator, "_post_one", side_effect=slow_post, return_value=0):
            results = await asyncio.gather(
                coordinator.run_posting(),
                coordinator.run_posting(),
            )
        # One should succeed (0), one should be skipped (2).
        assert results.count(0) == 1
        assert results.count(2) == 1

    @pytest.mark.asyncio
    async def test_generation_and_posting_can_run_concurrently(self, coordinator):
        """Generation and posting use separate locks — they can overlap."""
        gen_started = asyncio.Event()
        post_started = asyncio.Event()

        async def gen_fn():
            gen_started.set()
            await asyncio.sleep(0.1)

        # Add a pending post.
        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        async def slow_post(*args, **kwargs):
            post_started.set()
            await asyncio.sleep(0.1)

        with patch.object(coordinator, "_post_one", side_effect=slow_post):
            await asyncio.gather(
                coordinator.run_generation(gen_fn),
                coordinator.run_posting(),
            )
        assert gen_started.is_set()
        assert post_started.is_set()

    @pytest.mark.asyncio
    async def test_lock_released_on_exception(self, coordinator):
        """Lock must be released even if the generation function raises."""
        async def failing_gen():
            raise ValueError("boom")

        # First call raises.
        with pytest.raises(ValueError):
            await coordinator.run_generation(failing_gen)

        # Second call should succeed — lock was released.
        ran = await coordinator.run_generation(lambda: asyncio.sleep(0))
        assert ran is True

    @pytest.mark.asyncio
    async def test_posting_lock_released_on_exception(self, coordinator):
        """Lock must be released even if posting raises."""
        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        async def failing_post(*args, **kwargs):
            raise RuntimeError("post failed")

        with patch.object(coordinator, "_post_one", side_effect=failing_post):
            with pytest.raises(RuntimeError):
                await coordinator.run_posting()

        # Should be able to call again — lock was released.
        with patch.object(coordinator, "_post_one", return_value=0):
            result = await coordinator.run_posting()
        assert result == 0


class TestJobCoordinatorDrain:
    """Verify drain_posts consolidates the --once and dry-run paths."""

    @pytest.mark.asyncio
    async def test_drain_posts_all(self, coordinator, store):
        """Drain should post all pending posts and mark them posted."""
        for i in range(3):
            store.add_pending_post({"title": f"T{i}", "body": f"B{i}", "url": f"http://x.com/{i}"})

        # Dry-run mode (no BOT_TOKEN).
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOT_TOKEN", None)
            os.environ.pop("NEWS_CHANNEL_ID", None)
            result = await coordinator.drain_posts()

        assert result == 0
        assert store.count_pending() == 0

    @pytest.mark.asyncio
    async def test_drain_empty_queue(self, coordinator):
        """Drain with empty queue should return 0."""
        result = await coordinator.drain_posts()
        assert result == 0


class TestFormatPostMessage:
    """Verify format_post_message produces correct HTML."""

    def test_basic_message(self):
        msg = format_post_message("Title", "Body text", "https://example.com/article")
        assert "<b>Title</b>" in msg
        assert "Body text" in msg
        assert 'href="https://example.com/article"' in msg
        assert "Source: example.com" in msg

    def test_html_escaping(self):
        msg = format_post_message("<script>", "<b>body</b>", "https://example.com")
        assert "<script>" not in msg  # escaped
        assert "&lt;script&gt;" in msg
        assert "<b>body</b>" not in msg  # body escaped

    def test_no_url(self):
        msg = format_post_message("Title", "Body", "")
        assert "<a href" not in msg
        assert "Title" in msg
        assert "Body" in msg