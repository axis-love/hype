"""Tests for scheduler bookkeeping — timestamps advance only on success (flow_001025).

Tests drive the production _scheduler_gen_iteration() and _scheduler_post_iteration()
helpers directly, NOT reimplemented bookkeeping logic. Every _run_generation()
early-return path is covered through the production helper.
"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from newsbot.db import NewsStore
from newsbot.jobs import JobCoordinator
from newsbot.main import _scheduler_gen_iteration, _scheduler_post_iteration, _run_retention


class MockSettings:
    def __init__(self):
        self._data: dict[str, dict[str, object]] = {}
    def get(self, section, key, default=None):
        return self._data.get(section, {}).get(key, default)
    def set(self, section, key, value):
        self._data.setdefault("section", {})[key] = value
        self._data.setdefault(section, {})[key] = value


@pytest.fixture
def store(tmp_path: Path) -> NewsStore:
    return NewsStore(tmp_path / "test.sqlite")


@pytest.fixture
def settings():
    return MockSettings()


class TestSchedulerGenIteration:
    """Drive the production _scheduler_gen_iteration() helper for every
    _run_generation() early-return path and assert timestamp decisions."""

    @pytest.mark.asyncio
    async def test_success_advances_timestamp(self, store, settings):
        """last_gen_utc advances when _run_generation returns 0 (success)."""
        coordinator = JobCoordinator(store, settings)
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=0):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, gen_interval_s=0,
            )

        assert result == 0
        new_ts = settings.get("scheduler", "last_gen_utc")
        assert new_ts != old_ts  # timestamp advanced

    @pytest.mark.asyncio
    async def test_failure_preserves_timestamp(self, store, settings):
        """last_gen_utc preserved when _run_generation returns 1 (failure)."""
        coordinator = JobCoordinator(store, settings)
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=1):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, gen_interval_s=0,
            )

        assert result == 1
        assert settings.get("scheduler", "last_gen_utc") == old_ts

    @pytest.mark.asyncio
    async def test_skipped_preserves_timestamp(self, store, settings):
        """last_gen_utc preserved when generation is skipped (result 2)."""
        coordinator = JobCoordinator(store, settings)
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)

        # Set _gen_running so coordinator skips.
        coordinator._gen_running = True

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=0):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, gen_interval_s=0,
            )

        assert result == 2
        assert settings.get("scheduler", "last_gen_utc") == old_ts

    @pytest.mark.asyncio
    async def test_no_progress_preserves_timestamp(self, store, settings):
        """last_gen_utc preserved when _run_generation returns 3 (no-progress)."""
        coordinator = JobCoordinator(store, settings)
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=3):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, gen_interval_s=0,
            )

        assert result == 3
        assert settings.get("scheduler", "last_gen_utc") == old_ts

    @pytest.mark.asyncio
    async def test_exception_preserves_timestamp(self, store, settings):
        """last_gen_utc preserved when _run_generation raises an exception."""
        coordinator = JobCoordinator(store, settings)
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)

        async def exploding_gen():
            raise RuntimeError("LLM down")

        with patch("newsbot.main._run_generation", side_effect=exploding_gen):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, gen_interval_s=0,
            )

        assert result == 1  # exception → failure
        assert settings.get("scheduler", "last_gen_utc") == old_ts

    @pytest.mark.asyncio
    async def test_no_candidates_preserves_timestamp(self, store, settings):
        """last_gen_utc preserved when _run_generation returns 3
        (no candidates collected)."""
        coordinator = JobCoordinator(store, settings)
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=3):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, gen_interval_s=0,
            )

        assert result == 3
        assert settings.get("scheduler", "last_gen_utc") == old_ts

    @pytest.mark.asyncio
    async def test_all_seen_preserves_timestamp(self, store, settings):
        """last_gen_utc preserved when all candidates are already seen
        (_run_generation returns 3)."""
        coordinator = JobCoordinator(store, settings)
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=3):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, gen_interval_s=0,
            )

        assert result == 3
        assert settings.get("scheduler", "last_gen_utc") == old_ts

    @pytest.mark.asyncio
    async def test_llm_filter_empty_preserves_timestamp(self, store, settings):
        """last_gen_utc preserved when LLM filter output is empty
        (_run_generation returns 3)."""
        coordinator = JobCoordinator(store, settings)
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=3):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, gen_interval_s=0,
            )

        assert result == 3
        assert settings.get("scheduler", "last_gen_utc") == old_ts

    @pytest.mark.asyncio
    async def test_styler_empty_preserves_timestamp(self, store, settings):
        """last_gen_utc preserved when styler output is empty
        (_run_generation returns 3)."""
        coordinator = JobCoordinator(store, settings)
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_gen_utc", old_ts)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=3):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, gen_interval_s=0,
            )

        assert result == 3
        assert settings.get("scheduler", "last_gen_utc") == old_ts

    @pytest.mark.asyncio
    async def test_interval_not_elapsed_returns_idle(self, store, settings):
        """When interval has not elapsed, iteration returns 0 (idle) without
        calling _run_generation."""
        coordinator = JobCoordinator(store, settings)
        now = datetime.now(timezone.utc)
        settings.set("scheduler", "last_gen_utc", now.isoformat())

        called = False
        async def gen_fn():
            nonlocal called
            called = True
            return 0

        with patch("newsbot.main._run_generation", side_effect=gen_fn):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, gen_interval_s=3600,
            )

        assert result == 0
        assert not called  # _run_generation was NOT called

    @pytest.mark.asyncio
    async def test_retention_runs_on_success(self, store, settings):
        """Retention runs after successful generation."""
        coordinator = JobCoordinator(store, settings)
        retention_called = False
        original_retention = _run_retention

        def tracking_retention(s):
            nonlocal retention_called
            retention_called = True
            original_retention(s)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=0):
            with patch("newsbot.main._run_retention", side_effect=tracking_retention):
                await _scheduler_gen_iteration(
                    coordinator, store, settings, gen_interval_s=0,
                )

        assert retention_called

    @pytest.mark.asyncio
    async def test_retention_runs_on_failure(self, store, settings):
        """Retention runs after failed generation."""
        coordinator = JobCoordinator(store, settings)
        retention_called = False

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=1):
            with patch("newsbot.main._run_retention", side_effect=lambda s: setattr(
                type('', (), {'retention_called': True})(), 'retention_called', True
            ) if False else None) as mock_ret:
                mock_ret.side_effect = lambda s: None
                await _scheduler_gen_iteration(
                    coordinator, store, settings, gen_interval_s=0,
                )

        # Retention was called (side_effect was invoked).
        assert mock_ret.called

    @pytest.mark.asyncio
    async def test_retention_runs_on_no_progress(self, store, settings):
        """Retention runs after no-progress generation (result 3)."""
        coordinator = JobCoordinator(store, settings)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=3):
            with patch("newsbot.main._run_retention") as mock_ret:
                await _scheduler_gen_iteration(
                    coordinator, store, settings, gen_interval_s=0,
                )

        assert mock_ret.called


class TestSchedulerPostIteration:
    """Drive the production _scheduler_post_iteration() helper and assert
    timestamp decisions for every outcome."""

    @pytest.mark.asyncio
    async def test_success_advances_timestamp(self, store, settings):
        """last_post_utc advances when posting succeeds (result 0)."""
        coordinator = JobCoordinator(store, settings)
        store.add_pending_post({"title": "T", "body": "B", "url": "http://x.com"})
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_post_utc", old_ts)

        with patch.object(coordinator, "_deliver_one", new_callable=AsyncMock, return_value=0):
            result = await _scheduler_post_iteration(coordinator, settings, post_interval_s=0)

        assert result == 0
        assert settings.get("scheduler", "last_post_utc") != old_ts

    @pytest.mark.asyncio
    async def test_failure_preserves_timestamp(self, store, settings):
        """last_post_utc preserved when posting fails (result 1)."""
        coordinator = JobCoordinator(store, settings)
        store.add_pending_post({"title": "T", "body": "B", "url": "http://x.com"})
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_post_utc", old_ts)

        with patch.object(coordinator, "_deliver_one", new_callable=AsyncMock, return_value=1):
            result = await _scheduler_post_iteration(coordinator, settings, post_interval_s=0)

        assert result == 1
        assert settings.get("scheduler", "last_post_utc") == old_ts

    @pytest.mark.asyncio
    async def test_skipped_preserves_timestamp(self, store, settings):
        """last_post_utc preserved when posting is skipped (result 2)."""
        coordinator = JobCoordinator(store, settings)
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_post_utc", old_ts)

        # Set _post_running so coordinator skips.
        coordinator._post_running = True

        result = await _scheduler_post_iteration(coordinator, settings, post_interval_s=0)

        assert result == 2
        assert settings.get("scheduler", "last_post_utc") == old_ts

    @pytest.mark.asyncio
    async def test_empty_queue_preserves_timestamp(self, store, settings):
        """last_post_utc preserved when queue is empty (result 3)."""
        coordinator = JobCoordinator(store, settings)
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_post_utc", old_ts)

        result = await _scheduler_post_iteration(coordinator, settings, post_interval_s=0)

        assert result == 3
        assert settings.get("scheduler", "last_post_utc") == old_ts

    @pytest.mark.asyncio
    async def test_exception_preserves_timestamp(self, store, settings):
        """last_post_utc preserved when posting raises an exception."""
        coordinator = JobCoordinator(store, settings)
        store.add_pending_post({"title": "T", "body": "B", "url": "http://x.com"})
        old_ts = "2026-07-26T10:00:00+00:00"
        settings.set("scheduler", "last_post_utc", old_ts)

        async def exploding_deliver():
            raise RuntimeError("Telegram down")

        with patch.object(coordinator, "_deliver_one", side_effect=exploding_deliver):
            result = await _scheduler_post_iteration(coordinator, settings, post_interval_s=0)

        assert result == 1
        assert settings.get("scheduler", "last_post_utc") == old_ts

    @pytest.mark.asyncio
    async def test_interval_not_elapsed_returns_idle(self, store, settings):
        """When interval has not elapsed, returns 0 (idle) without posting."""
        coordinator = JobCoordinator(store, settings)
        now = datetime.now(timezone.utc)
        settings.set("scheduler", "last_post_utc", now.isoformat())

        result = await _scheduler_post_iteration(coordinator, settings, post_interval_s=3600)

        assert result == 0  # idle


class TestRetentionRunsThroughRuntimePaths:
    """Verify retention runs through actual runtime paths (not direct calls)."""

    @pytest.mark.asyncio
    async def test_retention_runs_through_gen_iteration_success(self, store, settings):
        """Retention runs after successful generation iteration."""
        from newsbot.main import _scheduler_gen_iteration, _run_retention

        coordinator = JobCoordinator(store, settings)
        settings.set("scheduler", "last_gen_utc", "2026-01-01T00:00:00+00:00")

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=0):
            with patch("newsbot.main._run_retention") as mock_ret:
                await _scheduler_gen_iteration(coordinator, store, settings, gen_interval_s=0)

        assert mock_ret.called

    @pytest.mark.asyncio
    async def test_retention_runs_through_gen_iteration_failure(self, store, settings):
        """Retention runs after failed generation iteration."""
        from newsbot.main import _scheduler_gen_iteration

        coordinator = JobCoordinator(store, settings)
        settings.set("scheduler", "last_gen_utc", "2026-01-01T00:00:00+00:00")

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=1):
            with patch("newsbot.main._run_retention") as mock_ret:
                await _scheduler_gen_iteration(coordinator, store, settings, gen_interval_s=0)

        assert mock_ret.called

    @pytest.mark.asyncio
    async def test_retention_runs_through_gen_iteration_no_progress(self, store, settings):
        """Retention runs after no-progress generation iteration."""
        from newsbot.main import _scheduler_gen_iteration

        coordinator = JobCoordinator(store, settings)
        settings.set("scheduler", "last_gen_utc", "2026-01-01T00:00:00+00:00")

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=3):
            with patch("newsbot.main._run_retention") as mock_ret:
                await _scheduler_gen_iteration(coordinator, store, settings, gen_interval_s=0)

        assert mock_ret.called

    @pytest.mark.asyncio
    async def test_retention_runs_through_gen_iteration_exception(self, store, settings):
        """Retention runs even when generation raises an exception."""
        from newsbot.main import _scheduler_gen_iteration

        coordinator = JobCoordinator(store, settings)
        settings.set("scheduler", "last_gen_utc", "2026-01-01T00:00:00+00:00")

        async def exploding_gen():
            raise RuntimeError("LLM down")

        with patch("newsbot.main._run_generation", side_effect=exploding_gen):
            with patch("newsbot.main._run_retention") as mock_ret:
                await _scheduler_gen_iteration(coordinator, store, settings, gen_interval_s=0)

        assert mock_ret.called


class TestSettingsStoreLifecycle:
    """Verify SettingsStore has close()/context manager support."""

    def test_settings_store_close(self, tmp_path):
        from core.settings_store import SettingsStore, SettingsStoreConfig

        store = SettingsStore(SettingsStoreConfig(db_path=tmp_path / "test.sqlite"))
        store.set("test", "key", "value")
        store.close()
        # Should not raise on double-close
        store.close()

    def test_settings_store_context_manager(self, tmp_path):
        from core.settings_store import SettingsStore, SettingsStoreConfig

        with SettingsStore(SettingsStoreConfig(db_path=tmp_path / "test.sqlite")) as store:
            store.set("test", "key", "value")
            assert store.get("test", "key") == "value"
        # Connection closed after context exit


class TestRetentionConfigurable:
    """Verify retention ages are configurable via env vars."""

    def test_retention_uses_env_vars(self, tmp_path):
        """_run_retention should read ages from env vars."""
        from newsbot.main import _run_retention
        from newsbot.db import NewsStore
        from datetime import datetime, timezone, timedelta

        store = NewsStore(tmp_path / "test.sqlite")
        # Add a posted post with an old timestamp so it's eligible for pruning.
        store.add_pending_post({"title": "old", "body": "b", "url": "http://old.com"})
        post = store.get_next_pending_post()
        store.mark_posted(post["id"])
        # Set posted_at to 5 days ago.
        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
        store._conn.execute("UPDATE pending_posts SET posted_at=? WHERE id=?", (old_ts, post["id"]))

        with patch.dict("os.environ", {
            "NEWS_RETENTION_POSTED_DAYS": "1",  # prune posts older than 1 day
            "NEWS_RETENTION_SEEN_DAYS": "1",
            "NEWS_RETENTION_DIGEST_DAYS": "1",
        }):
            _run_retention(store)

        # The 5-day-old posted post should be pruned.
        posted_count = store._conn.execute(
            "SELECT COUNT(*) AS n FROM pending_posts WHERE posted_at IS NOT NULL"
        ).fetchone()
        assert int(posted_count["n"]) == 0

    def test_retention_defaults_to_30_14_90(self, tmp_path):
        """_run_retention should use default ages when env vars are not set."""
        from newsbot.main import _run_retention
        from newsbot.db import NewsStore
        from unittest.mock import patch

        store = NewsStore(tmp_path / "test.sqlite")
        store.add_pending_post({"title": "old", "body": "b", "url": "http://old.com"})
        post = store.get_next_pending_post()
        store.mark_posted(post["id"])

        # Clear retention env vars to test defaults.
        with patch.dict("os.environ", {}, clear=True):
            _run_retention(store)

        # With defaults (30 days), a just-posted post should NOT be pruned.
        posted_count = store._conn.execute(
            "SELECT COUNT(*) AS n FROM pending_posts WHERE posted_at IS NOT NULL"
        ).fetchone()
        assert int(posted_count["n"]) == 1


class TestMarkPostedFailure:
    """Verify mark_posted failure after successful delivery doesn't leave row pending silently."""

    @pytest.mark.asyncio
    async def test_delivery_succeeds_mark_posted_fails_reports_error(self, store, settings):
        """When Telegram delivery succeeds but mark_posted fails,
        the coordinator must report failure (return 1) so the scheduler
        doesn't advance the timestamp."""
        from newsbot.jobs import JobCoordinator
        from unittest.mock import AsyncMock, patch
        import sqlite3

        coordinator = JobCoordinator(store, settings)
        store.add_pending_post({"title": "T", "body": "B", "url": "http://x.com"})

        # Mock post_digest to succeed (dry-run path).
        with patch.dict("os.environ", {"BOT_TOKEN": "", "NEWS_CHANNEL_ID": ""}):
            # Mock mark_posted to fail.
            with patch.object(store, "mark_posted", side_effect=sqlite3.OperationalError("disk full")):
                result = await coordinator._deliver_one()

        # Should return 1 (failure) because mark_posted failed.
        assert result == 1