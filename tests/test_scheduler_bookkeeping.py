"""Tests for wall-clock slot-based scheduler bookkeeping.

Drives the production _scheduler_gen_iteration() and _scheduler_post_iteration()
helpers directly with an injected ``now`` (slot keys are derived from local
wall-clock time via newsbot.clock). Slot rules:

  gen  — fires once per NEWS_GEN_HOURS slot; success consumes the slot;
         failure / no-progress / busy leaves it unconsumed (retry next tick);
         a missed slot still fires once after downtime (catch-up).
  post — even-hour slots only; success / empty / threshold-skip consume the
         slot; failure / busy leave it unconsumed (retry within the hour);
         missed slots are NEVER backfilled.
"""
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from newsbot.db import NewsStore
from newsbot.jobs import JobCoordinator
from newsbot.main import _scheduler_gen_iteration, _scheduler_post_iteration

TZ = ZoneInfo("Asia/Bangkok")
NOW = datetime(2026, 8, 16, 14, 30, tzinfo=TZ)  # even hour, between 05 and 17 gen slots


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


class TestSchedulerGenIteration:
    """Slot-based generation scheduling with catch-up."""

    @pytest.mark.asyncio
    async def test_success_consumes_due_slot(self, store, settings):
        """Success writes scheduler.last_gen_slot = the due slot."""
        coordinator = JobCoordinator(store, settings)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=0):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, [5, 17], now=NOW,
            )

        assert result == 0
        assert settings.get("scheduler", "last_gen_slot") == "2026-08-16T05"

    @pytest.mark.asyncio
    async def test_second_tick_same_slot_is_idle(self, store, settings):
        """Once the slot is consumed, further ticks within the slot are idle
        and do NOT invoke generation."""
        coordinator = JobCoordinator(store, settings)
        settings.set("scheduler", "last_gen_slot", "2026-08-16T05")

        called = False

        async def gen_fn(*args):
            nonlocal called
            called = True
            return 0

        with patch("newsbot.main._run_generation", side_effect=gen_fn):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, [5, 17], now=NOW,
            )

        assert result == 0
        assert not called

    @pytest.mark.asyncio
    async def test_failure_leaves_slot_unconsumed(self, store, settings):
        """Failure leaves the slot unconsumed so the next tick retries."""
        coordinator = JobCoordinator(store, settings)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=1):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, [5, 17], now=NOW,
            )

        assert result == 1
        assert settings.get("scheduler", "last_gen_slot", default="") == ""

    @pytest.mark.asyncio
    async def test_no_progress_leaves_slot_unconsumed(self, store, settings):
        """No-progress (3) leaves the slot unconsumed."""
        coordinator = JobCoordinator(store, settings)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=3):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, [5, 17], now=NOW,
            )

        assert result == 3
        assert settings.get("scheduler", "last_gen_slot", default="") == ""

    @pytest.mark.asyncio
    async def test_busy_leaves_slot_unconsumed(self, store, settings):
        """Already-running generation (2) leaves the slot unconsumed."""
        coordinator = JobCoordinator(store, settings)
        coordinator._gen_running = True

        result = await _scheduler_gen_iteration(
            coordinator, store, settings, [5, 17], now=NOW,
        )

        assert result == 2
        assert settings.get("scheduler", "last_gen_slot", default="") == ""

    @pytest.mark.asyncio
    async def test_exception_leaves_slot_unconsumed(self, store, settings):
        """An exception counts as failure — slot unconsumed."""
        coordinator = JobCoordinator(store, settings)

        async def exploding_gen(*args):
            raise RuntimeError("LLM down")

        with patch("newsbot.main._run_generation", side_effect=exploding_gen):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, [5, 17], now=NOW,
            )

        assert result == 1
        assert settings.get("scheduler", "last_gen_slot", default="") == ""

    @pytest.mark.asyncio
    async def test_catch_up_after_downtime(self, store, settings):
        """last_gen_slot two days old + now=14:00 → gen fires ONCE for
        today's 05 slot (the most recent due slot), not for every missed one."""
        coordinator = JobCoordinator(store, settings)
        settings.set("scheduler", "last_gen_slot", "2026-08-14T17")

        runs = 0

        async def gen_fn(*args):
            nonlocal runs
            runs += 1
            return 0

        with patch("newsbot.main._run_generation", side_effect=gen_fn):
            result = await _scheduler_gen_iteration(
                coordinator, store, settings, [5, 17], now=NOW,
            )
            # Second tick in the same slot: idle, no second run.
            result2 = await _scheduler_gen_iteration(
                coordinator, store, settings, [5, 17], now=NOW,
            )

        assert result == 0 and result2 == 0
        assert runs == 1
        assert settings.get("scheduler", "last_gen_slot") == "2026-08-16T05"

    @pytest.mark.asyncio
    async def test_before_first_gen_hour_due_slot_is_yesterday(self, store, settings):
        """At 03:00 the most recent due slot is yesterday's 17:00."""
        coordinator = JobCoordinator(store, settings)

        with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=0):
            await _scheduler_gen_iteration(
                coordinator, store, settings, [5, 17],
                now=datetime(2026, 8, 16, 3, 0, tzinfo=TZ),
            )

        assert settings.get("scheduler", "last_gen_slot") == "2026-08-15T17"

    @pytest.mark.asyncio
    async def test_retention_runs_on_every_outcome(self, store, settings):
        """Retention runs on success, failure, no-progress, and exception."""
        coordinator = JobCoordinator(store, settings)

        for gen_result in (0, 1, 3):
            fresh = MockSettings()  # unconsumed slot each round
            with patch("newsbot.main._run_generation", new_callable=AsyncMock, return_value=gen_result):
                with patch("newsbot.main._run_retention") as mock_ret:
                    await _scheduler_gen_iteration(
                        coordinator, store, fresh, [5, 17], now=NOW,
                    )
            assert mock_ret.called, f"retention skipped for result {gen_result}"

        async def exploding_gen(*args):
            raise RuntimeError("boom")

        with patch("newsbot.main._run_generation", side_effect=exploding_gen):
            with patch("newsbot.main._run_retention") as mock_ret:
                await _scheduler_gen_iteration(
                    coordinator, store, MockSettings(), [5, 17], now=NOW,
                )
        assert mock_ret.called


class TestSchedulerPostIteration:
    """Slot-based posting scheduling: even hours, no backfill."""

    @pytest.mark.asyncio
    async def test_success_consumes_slot(self, store, settings):
        coordinator = JobCoordinator(store, settings)

        with patch.object(coordinator, "_deliver_one", new_callable=AsyncMock, return_value=0):
            result = await _scheduler_post_iteration(coordinator, settings, now=NOW)

        assert result == 0
        assert settings.get("scheduler", "last_post_slot") == "2026-08-16T14"

    @pytest.mark.asyncio
    async def test_odd_hour_is_idle(self, store, settings):
        """Odd hours have no post slot — idle, nothing invoked."""
        coordinator = JobCoordinator(store, settings)

        with patch.object(coordinator, "_deliver_one", new_callable=AsyncMock, return_value=0) as mock_deliver:
            result = await _scheduler_post_iteration(
                coordinator, settings, now=NOW.replace(hour=13),
            )

        assert result == 0
        assert not mock_deliver.called
        assert settings.get("scheduler", "last_post_slot", default="") == ""

    @pytest.mark.asyncio
    async def test_second_tick_same_slot_is_idle(self, store, settings):
        coordinator = JobCoordinator(store, settings)
        settings.set("scheduler", "last_post_slot", "2026-08-16T14")

        with patch.object(coordinator, "_deliver_one", new_callable=AsyncMock, return_value=0) as mock_deliver:
            result = await _scheduler_post_iteration(coordinator, settings, now=NOW)

        assert result == 0
        assert not mock_deliver.called

    @pytest.mark.asyncio
    async def test_failure_leaves_slot_unconsumed(self, store, settings):
        """Failure (1) → retry within the hour; slot not consumed."""
        coordinator = JobCoordinator(store, settings)

        with patch.object(coordinator, "_deliver_one", new_callable=AsyncMock, return_value=1):
            result = await _scheduler_post_iteration(coordinator, settings, now=NOW)

        assert result == 1
        assert settings.get("scheduler", "last_post_slot", default="") == ""

    @pytest.mark.asyncio
    async def test_busy_leaves_slot_unconsumed(self, store, settings):
        """Already posting (2) → slot not consumed."""
        coordinator = JobCoordinator(store, settings)
        coordinator._post_running = True

        result = await _scheduler_post_iteration(coordinator, settings, now=NOW)

        assert result == 2
        assert settings.get("scheduler", "last_post_slot", default="") == ""

    @pytest.mark.asyncio
    async def test_empty_store_consumes_slot(self, store, settings):
        """Code 3 (nothing to deliver) consumes the slot — healthy skip."""
        coordinator = JobCoordinator(store, settings)

        result = await _scheduler_post_iteration(coordinator, settings, now=NOW)

        assert result == 3
        assert settings.get("scheduler", "last_post_slot") == "2026-08-16T14"

    @pytest.mark.asyncio
    async def test_threshold_skip_consumes_slot(self, store, settings):
        """Code 4 (nothing hot enough) consumes the slot — healthy skip."""
        coordinator = JobCoordinator(store, settings)

        with patch.object(coordinator, "_deliver_one", new_callable=AsyncMock, return_value=4):
            result = await _scheduler_post_iteration(coordinator, settings, now=NOW)

        assert result == 4
        assert settings.get("scheduler", "last_post_slot") == "2026-08-16T14"

    @pytest.mark.asyncio
    async def test_exception_leaves_slot_unconsumed(self, store, settings):
        coordinator = JobCoordinator(store, settings)

        async def exploding_deliver():
            raise RuntimeError("Telegram down")

        with patch.object(coordinator, "_deliver_one", side_effect=exploding_deliver):
            result = await _scheduler_post_iteration(coordinator, settings, now=NOW)

        assert result == 1
        assert settings.get("scheduler", "last_post_slot", default="") == ""

    @pytest.mark.asyncio
    async def test_missed_slot_not_backfilled(self, store, settings):
        """Post slot missed during downtime is NOT backfilled: with no key
        at all and now=15:00 (odd, slot already over), nothing fires; at the
        NEXT even hour exactly the new slot fires — not the missed one."""
        coordinator = JobCoordinator(store, settings)

        # now = 15:30 odd hour → idle even though 14:00 slot was never consumed.
        with patch.object(coordinator, "_deliver_one", new_callable=AsyncMock, return_value=0) as mock_deliver:
            result = await _scheduler_post_iteration(
                coordinator, settings, now=NOW.replace(hour=15),
            )
        assert result == 0
        assert not mock_deliver.called

        # next even hour fires exactly once, for the new slot.
        with patch.object(coordinator, "_deliver_one", new_callable=AsyncMock, return_value=0):
            await _scheduler_post_iteration(
                coordinator, settings, now=NOW.replace(hour=16),
            )
        assert settings.get("scheduler", "last_post_slot") == "2026-08-16T16"

    @pytest.mark.asyncio
    async def test_restart_same_slot_no_double_post(self, store, settings):
        """Restart after a successful post does not refire the same slot."""
        coordinator = JobCoordinator(store, settings)
        settings.set("scheduler", "last_post_slot", "2026-08-16T14")

        with patch.object(coordinator, "_deliver_one", new_callable=AsyncMock, return_value=0) as mock_deliver:
            result = await _scheduler_post_iteration(coordinator, settings, now=NOW)

        assert result == 0
        assert not mock_deliver.called


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
        post = store.list_unposted_posts()[0]
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
        post = store.list_unposted_posts()[0]
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
        from tests.helpers import scored_story, echo_style
        import sqlite3

        coordinator = JobCoordinator(store, settings)
        store.add_stories_to_store([scored_story("T", 90.0)], [])

        # Mock post_digest to succeed (dry-run path).
        with patch.dict("os.environ", {"BOT_TOKEN": "", "NEWS_CHANNEL_ID": ""}), \
             patch("newsbot.jobs.llm_style_posts", new=echo_style), \
             patch("newsbot.jobs._build_lm_client", return_value=object()):
            # Mock mark_posted to fail.
            with patch.object(store, "mark_posted", side_effect=sqlite3.OperationalError("disk full")):
                result = await coordinator._deliver_one()

        # Delivery succeeded but bookkeeping failed → report failure.
        assert result == 1
