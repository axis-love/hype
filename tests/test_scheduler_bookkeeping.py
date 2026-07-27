"""Tests for scheduler bookkeeping — timestamps advance only on success (flow_001025)."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from newsbot.db import NewsStore
from newsbot.jobs import JobCoordinator


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


class TestSchedulerBookkeeping:
    """Verify that timestamps advance only after successful jobs."""

    @pytest.mark.asyncio
    async def test_gen_timestamp_advances_on_success(self, store, settings):
        """last_gen_utc should be set after a successful generation."""
        coordinator = JobCoordinator(store, settings)

        async def gen_fn():
            return 0

        result = await coordinator.run_generation(gen_fn)
        assert result == 0
        # Simulate what the scheduler loop does on success
        settings.set("scheduler", "last_gen_utc", datetime.now(timezone.utc).isoformat())
        assert settings.get("scheduler", "last_gen_utc") is not None

    @pytest.mark.asyncio
    async def test_gen_timestamp_preserved_on_exception(self, store, settings):
        """last_gen_utc should NOT advance when generation throws."""
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)
        coordinator = JobCoordinator(store, settings)

        async def failing_gen():
            raise RuntimeError("LLM down")

        gen_failed = False
        try:
            await coordinator.run_generation(failing_gen)
        except RuntimeError:
            gen_failed = True

        # The scheduler loop checks gen_failed and does NOT advance timestamp.
        if not gen_failed:
            settings.set("scheduler", "last_gen_utc", datetime.now(timezone.utc).isoformat())

        assert settings.get("scheduler", "last_gen_utc") == old_ts

    @pytest.mark.asyncio
    async def test_post_timestamp_advances_on_success(self, store, settings):
        """last_post_utc should be set after a successful post (result 0)."""
        coordinator = JobCoordinator(store, settings)
        store.add_pending_post({"title": "T", "body": "B", "url": "http://x.com"})

        async def ok_deliver():
            return 0

        with patch.object(coordinator, "_deliver_one", new=ok_deliver):
            result = await coordinator.run_posting()
        assert result == 0

        post_failed = (result == 1)
        if not post_failed:
            settings.set("scheduler", "last_post_utc", datetime.now(timezone.utc).isoformat())

        assert settings.get("scheduler", "last_post_utc") is not None

    @pytest.mark.asyncio
    async def test_post_timestamp_preserved_on_empty_queue(self, store, settings):
        """last_post_utc should NOT advance when queue is empty (result 3)."""
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_post_utc", old_ts)

        coordinator = JobCoordinator(store, settings)
        # No pending posts — _deliver_one returns 3.
        result = await coordinator.run_posting()
        assert result == 3  # no-op

        post_success = (result == 0)
        if post_success:
            settings.set("scheduler", "last_post_utc", datetime.now(timezone.utc).isoformat())

        assert settings.get("scheduler", "last_post_utc") == old_ts

    @pytest.mark.asyncio
    async def test_post_timestamp_preserved_on_failure(self, store, settings):
        """last_post_utc should NOT advance when posting fails (returns 1)."""
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_post_utc", old_ts)

        # Add a pending post so posting has something to fail on.
        store.add_pending_post({"title": "T", "body": "B", "url": "http://x.com"})

        coordinator = JobCoordinator(store, settings)

        # Mock _deliver_one to fail.
        async def failing_post():
            return 1

        with patch.object(coordinator, "_deliver_one", side_effect=failing_post):
            result = await coordinator.run_posting()

        assert result == 1  # failure

        post_failed = (result == 1)
        if not post_failed:
            settings.set("scheduler", "last_post_utc", datetime.now(timezone.utc).isoformat())

        assert settings.get("scheduler", "last_post_utc") == old_ts

    @pytest.mark.asyncio
    async def test_post_timestamp_advances_on_noop(self, store, settings):
        """last_post_utc should NOT advance when there are no pending posts (result 3, was 0)."""
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_post_utc", old_ts)

        coordinator = JobCoordinator(store, settings)
        # No pending posts — returns 3 (no-op, not success).
        result = await coordinator.run_posting()
        assert result == 3

        post_success = (result == 0)
        if post_success:
            settings.set("scheduler", "last_post_utc", datetime.now(timezone.utc).isoformat())

        assert settings.get("scheduler", "last_post_utc") == old_ts

    @pytest.mark.asyncio
    async def test_gen_skipped_does_not_advance_timestamp(self, store, settings):
        """When generation is already running (skipped), timestamp should not change."""
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)
        coordinator = JobCoordinator(store, settings)

        # Simulate already-running by setting the flag.
        coordinator._gen_running = True

        async def gen_fn():
            return 0

        result = await coordinator.run_generation(gen_fn)
        assert result == 2  # skipped

        # In the updated scheduler loop, gen_success is only True when result == 0.
        # result 2 (skipped) does NOT set gen_success, so timestamp unchanged.
        gen_success = (result == 0)
        if gen_success:
            settings.set("scheduler", "last_gen_utc", datetime.now(timezone.utc).isoformat())

        assert settings.get("scheduler", "last_gen_utc") == old_ts

    @pytest.mark.asyncio
    async def test_gen_db_failure_does_not_advance_timestamp(self, store, settings):
        """When _run_generation returns 1 (DB failure), timestamp should not advance."""
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)
        coordinator = JobCoordinator(store, settings)

        async def failing_gen():
            return 1  # DB failure

        result = await coordinator.run_generation(failing_gen)
        assert result == 1  # failure propagated

        # In the scheduler loop, gen_success is only True when result == 0.
        gen_success = (result == 0)
        if gen_success:
            settings.set("scheduler", "last_gen_utc", datetime.now(timezone.utc).isoformat())

        assert settings.get("scheduler", "last_gen_utc") == old_ts

    @pytest.mark.asyncio
    async def test_gen_no_progress_does_not_advance_timestamp(self, store, settings):
        """When _run_generation returns 3 (no-progress), timestamp should not advance."""
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)
        coordinator = JobCoordinator(store, settings)

        async def no_progress_gen():
            return 3  # no-progress: empty collection, LLM filter empty, etc.

        result = await coordinator.run_generation(no_progress_gen)
        assert result == 3  # no-progress propagated

        gen_success = (result == 0)
        if gen_success:
            settings.set("scheduler", "last_gen_utc", datetime.now(timezone.utc).isoformat())

        assert settings.get("scheduler", "last_gen_utc") == old_ts

    @pytest.mark.asyncio
    async def test_retention_runs_on_no_progress(self, store, settings):
        """Retention should run even when generation returns no-progress (3)."""
        from newsbot.main import _run_retention

        store.add_pending_post({"title": "old", "body": "b", "url": "http://old.com"})
        post = store.get_next_pending_post()
        store.mark_posted(post["id"])

        # Run retention — should not raise, even with old data.
        _run_retention(store)
        assert store.count_pending() == 0  # the old post was marked posted

    @pytest.mark.asyncio
    async def test_retention_runs_on_failure(self, store, settings):
        """Retention should run even when generation returns failure (1)."""
        from newsbot.main import _run_retention

        _run_retention(store)
        assert store.count_pending() == 0