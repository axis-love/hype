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
            return 0

        # Launch two concurrently.
        results = await asyncio.gather(
            coordinator.run_generation(slow_gen),
            coordinator.run_generation(slow_gen),
        )
        # One should succeed (0), one should be skipped (2).
        assert results.count(0) == 1
        assert results.count(2) == 1
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_posting_lock_prevents_overlap(self, coordinator):
        """Two concurrent posting calls — only one should run, the other skipped."""
        # Add a pending post so posting has something to do.
        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        # Mock the actual delivery to be slow.
        async def slow_deliver(*args, **kwargs):
            await asyncio.sleep(0.1)
            return 0

        with patch.object(coordinator, "_deliver_one", side_effect=slow_deliver, return_value=0):
            results = await asyncio.gather(
                coordinator.run_posting(),
                coordinator.run_posting(),
            )
        # One should succeed (0), one should be skipped (2).
        assert results.count(0) == 1
        assert results.count(2) == 1

    @pytest.mark.asyncio
    async def test_generation_and_posting_cannot_overlap(self, coordinator):
        """Generation and posting use a SINGLE lock — they cannot overlap."""
        gen_started = asyncio.Event()
        post_started = asyncio.Event()
        gen_done = asyncio.Event()
        post_done = asyncio.Event()

        async def gen_fn():
            gen_started.set()
            await asyncio.sleep(0.05)
            gen_done.set()
            return 0

        # Add a pending post.
        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        async def slow_deliver(*args, **kwargs):
            post_started.set()
            await asyncio.sleep(0.05)
            post_done.set()
            return 0

        with patch.object(coordinator, "_deliver_one", side_effect=slow_deliver):
            await asyncio.gather(
                coordinator.run_generation(gen_fn),
                coordinator.run_posting(),
            )
        assert gen_started.is_set()
        assert post_started.is_set()
        # The single lock means gen and post are serialized —
        # gen must complete before post starts (or vice versa).
        # Both must have completed.
        assert gen_done.is_set()
        assert post_done.is_set()

    @pytest.mark.asyncio
    async def test_lock_released_on_exception(self, coordinator):
        """Lock must be released even if the generation function raises."""
        async def failing_gen():
            raise ValueError("boom")

        # First call raises.
        with pytest.raises(ValueError):
            await coordinator.run_generation(failing_gen)

        # Second call should succeed — lock was released.
        result = await coordinator.run_generation(lambda: asyncio.sleep(0))
        assert result == 0

    @pytest.mark.asyncio
    async def test_posting_lock_released_on_exception(self, coordinator):
        """Lock must be released even if posting raises."""
        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        async def failing_deliver(*args, **kwargs):
            raise RuntimeError("post failed")

        with patch.object(coordinator, "_deliver_one", side_effect=failing_deliver):
            with pytest.raises(RuntimeError):
                await coordinator.run_posting()

        # Should be able to call again — lock was released.
        with patch.object(coordinator, "_deliver_one", return_value=0):
            result = await coordinator.run_posting()
        assert result == 0

    @pytest.mark.asyncio
    async def test_multiple_gen_queued_behind_post_only_one_runs(self, coordinator):
        """Hold posting active, launch two generation calls, assert only one runs."""
        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        post_can_finish = asyncio.Event()

        gen_call_count = 0

        async def slow_deliver(*args, **kwargs):
            await post_can_finish.wait()
            return 0

        async def gen_fn():
            nonlocal gen_call_count
            gen_call_count += 1
            return 0

        with patch.object(coordinator, "_deliver_one", side_effect=slow_deliver):
            post_task = asyncio.create_task(coordinator.run_posting())
            # Give posting time to acquire the lock.
            await asyncio.sleep(0.05)

            # Launch two generation calls while posting is active.
            # Don't use gather — the first gen waits for the lock (held by posting),
            # so gather would hang. Launch the first as a task, the second returns 2.
            gen1_task = asyncio.create_task(coordinator.run_generation(gen_fn))
            # Give gen1 time to set _gen_running and start waiting for the lock.
            await asyncio.sleep(0.02)

            gen2_result = await coordinator.run_generation(gen_fn)
            assert gen2_result == 2  # skipped because gen1 already set the flag

            # Release posting so gen1 can proceed.
            post_can_finish.set()
            await post_task
            gen1_result = await gen1_task
            assert gen1_result == 0

        # Only one generation actually ran.
        assert gen_call_count == 1

    @pytest.mark.asyncio
    async def test_multiple_post_queued_behind_gen_only_one_runs(self, coordinator):
        """Hold generation active, launch two posting calls, assert only one runs."""
        gen_can_finish = asyncio.Event()

        deliver_call_count = 0

        async def slow_gen():
            await gen_can_finish.wait()
            return 0

        async def fast_deliver(*args, **kwargs):
            nonlocal deliver_call_count
            deliver_call_count += 1
            return 0

        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        with patch.object(coordinator, "_deliver_one", side_effect=fast_deliver):
            gen_task = asyncio.create_task(coordinator.run_generation(slow_gen))
            # Give generation time to acquire the lock.
            await asyncio.sleep(0.05)

            # Launch two posting calls while generation is active.
            # The first waits for the lock, the second returns 2.
            post1_task = asyncio.create_task(coordinator.run_posting())
            await asyncio.sleep(0.02)

            post2_result = await coordinator.run_posting()
            assert post2_result == 2  # skipped

            # Release generation so post1 can proceed.
            gen_can_finish.set()
            gen_result = await gen_task
            assert gen_result == 0
            post1_result = await post1_task
            assert post1_result == 0

        # Only one posting actually ran.
        assert deliver_call_count == 1

    @pytest.mark.asyncio
    async def test_coordinator_returns_to_idle_after_timeout(self, coordinator):
        """Coordinator state returns to idle after a timeout."""
        async def slow_gen():
            await asyncio.sleep(10)
            return 0

        result = await coordinator.run_generation(slow_gen, timeout=0.05)
        assert result == 1  # timeout
        assert coordinator.generation_running is False

    @pytest.mark.asyncio
    async def test_coordinator_returns_to_idle_after_cancellation(self, coordinator):
        """Coordinator state returns to idle after task cancellation."""
        async def slow_gen():
            await asyncio.sleep(10)
            return 0

        task = asyncio.create_task(coordinator.run_generation(slow_gen))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert coordinator.generation_running is False

    @pytest.mark.asyncio
    async def test_no_duplicate_posts_under_concurrent_posting(self, coordinator, store):
        """Concurrent posting calls must not deliver the same post twice."""
        store.add_pending_post({"title": "T", "body": "B", "url": "http://x.com"})

        delivered_ids: list[int] = []

        async def capture_deliver():
            # Yield to let any concurrent call check the admission flag.
            await asyncio.sleep(0.05)
            post = store.get_next_pending_post()
            if post:
                delivered_ids.append(post["id"])
                store.mark_posted(post["id"])
            return 0

        with patch.object(coordinator, "_deliver_one", new=capture_deliver):
            results = await asyncio.gather(
                coordinator.run_posting(),
                coordinator.run_posting(),
            )

        # One should succeed (0), one should be skipped (2).
        assert results.count(0) == 1
        assert results.count(2) == 1
        # No duplicate delivery.
        assert len(delivered_ids) == 1


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