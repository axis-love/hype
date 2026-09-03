"""Tests for flow_001124: same-topic cooldown in pick_hottest.

Covers:
  1a. Hottest row excluded -> next-hottest eligible non-excluded row wins.
  1b. All above-threshold rows excluded -> skip result, no crash.
  1c. Empty exclusion set == current behaviour bit-for-bit.
  1d. Excluded rows still count toward median.
  2.  Jobs-level test with stubbed store: 3 gaming posts in 24h + cooldown
      -> 4th gaming row not picked, colder non-gaming row is; posts older
      than 24h don't count; NULL origin_topic unaffected; 0 disables.
  3.  selection.py still imports nothing beyond stdlib + newsbot.scoring.
  4.  post_pick/post_skip log events carry the cooldown-excluded count.
  5.  README documents NEWS_TOPIC_COOLDOWN_MAX.
"""
from __future__ import annotations

import ast
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from newsbot.db import NewsStore
from newsbot.jobs import JobCoordinator
from newsbot.selection import PickResult, pick_hottest


def _mark_posted(store: NewsStore, row_id: int, posted_at: str) -> None:
    """Mark a row as posted with a specific timestamp (dual-write).

    Sets posted_at on pending_posts AND inserts a 'telegram' delivery
    row with the same timestamp, mirroring mark_posted but allowing
    a custom timestamp for cooldown window-testing.
    """
    store._conn.execute(
        "UPDATE pending_posts SET posted_at=? WHERE id=?",
        (posted_at, row_id),
    )
    store._conn.execute(
        "INSERT OR IGNORE INTO deliveries(post_id, channel, delivered_at, message_id) "
        "VALUES(?,?,?,?)",
        (row_id, "telegram", posted_at, None),
    )


# --- selection.py test helpers (same as test_selection.py) -----------------

CFG = {"lookback_hours": 48}
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
FLOOR, RATIO, BONUS, CAP = 35.0, 0.5, 0.2, 2.0


def _row(row_id: int, temp: float, merge_count: int = 1) -> dict:
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
        "lookback_hours": 48,
        "merge_count": merge_count,
    }


def _pick(rows, **kw) -> PickResult:
    return pick_hottest(
        rows,
        CFG,
        now=NOW,
        floor=kw.pop("floor", FLOOR),
        ratio=kw.pop("ratio", RATIO),
        merge_bonus=kw.pop("merge_bonus", BONUS),
        merge_cap=kw.pop("merge_cap", CAP),
        **kw,
    )


# --- AC 1: Unit tests for pick_hottest with excluded_ids -------------------


class TestPickHottestExcludedIds:
    """AC 1: excluded_ids parameter behavior."""

    def test_hottest_excluded_next_wins(self):
        """1a: hottest row excluded -> next-hottest eligible non-excluded wins."""
        rows = [_row(1, 90.0), _row(2, 70.0), _row(3, 50.0)]
        result = _pick(rows, excluded_ids={1})
        assert result.reason == "picked"
        assert result.row["id"] == 2, "second-hottest should win when hottest excluded"

    def test_all_eligible_excluded_gives_below_threshold(self):
        """1b: all above-threshold rows excluded -> skip, no crash."""
        rows = [_row(1, 90.0), _row(2, 70.0)]
        # threshold = max(35, 0.5 * 80) = 40; both eligible, both excluded.
        result = _pick(rows, excluded_ids={1, 2})
        assert result.reason == "below_threshold"
        assert result.row is None
        # temps still populated for both rows.
        assert set(result.temps.keys()) == {1, 2}

    def test_empty_exclusion_bit_for_bit_current(self):
        """1c: empty exclusion set == current behaviour bit-for-bit."""
        rows = [_row(1, 50.0), _row(2, 90.0), _row(3, 70.0)]
        without = _pick(rows)
        with_empty = _pick(rows, excluded_ids=set())
        assert without.reason == with_empty.reason
        assert without.row["id"] == with_empty.row["id"]
        assert without.threshold == with_empty.threshold
        assert without.median == with_empty.median
        assert without.hottest == with_empty.hottest

    def test_none_exclusion_bit_for_bit_current(self):
        """1c: None exclusion == current behaviour (default param)."""
        rows = [_row(1, 50.0), _row(2, 90.0)]
        without = _pick(rows)
        with_none = _pick(rows, excluded_ids=None)
        assert without.row["id"] == with_none.row["id"]

    def test_excluded_still_count_toward_median(self):
        """1d: excluded rows still participate in temps/median."""
        rows = [_row(1, 90.0), _row(2, 10.0), _row(3, 50.0)]
        # Exclude row 1 (hottest). median should still include row 1's temp.
        result = _pick(rows, excluded_ids={1})
        # median([90, 10, 50]) = 50.0; threshold = max(35, 25) = 35.
        assert result.median == 50.0
        assert result.hottest == 90.0
        assert 1 in result.temps
        assert result.temps[1] == 90.0


# --- AC 2: Jobs-level test with stubbed store ------------------------------


def _scored_story_topic(
    title: str, engagement: float, *, topic: str | None,
    hours_old: float = 1.0,
) -> dict:
    """Build a scored store story with a specific origin_topic."""
    now = datetime.now(timezone.utc)
    published = (now - timedelta(hours=hours_old)).isoformat(timespec="seconds")
    bd = {
        "source": "hn",
        "published_at": published,
        "upvotes": int(engagement),
        "comments": 1,
        "stars": 0,
        "reposts": 0,
        "crosspost_count": 1,
        "penalty": 1.0,
        "lookback_hours": 48,
        "score": engagement,
        "engagement": engagement,
        "recency": 1.0,
        "source_weight": 1.0,
        "topic_bonus": 0,
        "crosspost_bonus": 0.0,
        "matched_topics": [],
        "origin_topic": topic,
        "scored_at": published,
    }
    return {
        "title": title,
        "url": f"https://example.com/{title.lower().replace(' ', '-')}",
        "source": "hn",
        "source_name": "Hacker News",
        "snippet": f"Snippet for {title}",
        "upvotes": int(engagement),
        "comments": 1,
        "published_at": published,
        "score_breakdown": bd,
    }


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    s = NewsStore(tmp_path / "cooldown.sqlite")
    yield s
    s.close()


@pytest.fixture
def settings():
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


class _frozen_dt:
    """Stand-in for datetime whose now() returns a fixed instant."""
    def __init__(self, fixed: datetime):
        self._fixed = fixed
    def now(self, tz=None):
        return self._fixed if tz else self._fixed.replace(tzinfo=None)
    def __getattr__(self, name):
        return getattr(datetime, name)


COOLDOWN_NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


class TestTopicCooldown:
    """AC 2: jobs-level test with stubbed store."""

    @pytest.mark.asyncio
    async def test_fourth_gaming_row_not_picked(self, coordinator, store, monkeypatch):
        """3 gaming posts within 24h + NEWS_TOPIC_COOLDOWN_MAX=3 -> 4th
        gaming row excluded, colder non-gaming row picked instead."""
        monkeypatch.setattr("newsbot.jobs.datetime", _frozen_dt(COOLDOWN_NOW))
        monkeypatch.setenv("NEWS_TOPIC_COOLDOWN_MAX", "3")

        # Insert 3 gaming stories and mark them posted within 24h.
        for i in range(3):
            s = _scored_story_topic(f"Gaming{i}", 100.0 - i * 5, topic="gaming")
            store.add_stories_to_store([s], [])
            rid = store._conn.execute(
                "SELECT id FROM pending_posts WHERE title=?", (f"Gaming{i}",)
            ).fetchone()["id"]
            _mark_posted(store, rid, (COOLDOWN_NOW - timedelta(hours=i + 1)).isoformat(timespec="seconds"))

        # Insert a 4th gaming row (hot) and a non-gaming row (colder).
        gaming4 = _scored_story_topic("Gaming4 Hot", 90.0, topic="gaming")
        ai_row = _scored_story_topic("AI News", 60.0, topic="ai")
        store.add_stories_to_store([gaming4, ai_row], [])

        picked = []
        async def fake_style(items, lm, **kw):
            picked.append(items[0].get("title"))
            return [{"title": "S", "body": "B"}]

        with patch("newsbot.jobs.llm_style_posts", new=fake_style), \
             patch("newsbot.jobs._build_lm_client", return_value=object()):
            os.environ.pop("BOT_TOKEN", None)
            os.environ.pop("NEWS_CHANNEL_ID", None)
            result = await coordinator.run_posting()

        assert result == 0
        # Gaming4 must NOT be picked (3 gaming posts in 24h, cooldown=3).
        # AI News (colder, non-gaming) should be picked instead.
        assert picked == ["AI News"], f"expected AI News picked, got {picked}"

    @pytest.mark.asyncio
    async def test_posts_older_than_24h_dont_count(self, coordinator, store, monkeypatch):
        """Posts older than 24h should not count toward cooldown."""
        monkeypatch.setattr("newsbot.jobs.datetime", _frozen_dt(COOLDOWN_NOW))
        monkeypatch.setenv("NEWS_TOPIC_COOLDOWN_MAX", "3")

        # Insert 3 gaming stories posted > 24h ago.
        for i in range(3):
            s = _scored_story_topic(f"OldGaming{i}", 100.0, topic="gaming")
            store.add_stories_to_store([s], [])
            rid = store._conn.execute(
                "SELECT id FROM pending_posts WHERE title=?", (f"OldGaming{i}",)
            ).fetchone()["id"]
            _mark_posted(store, rid, (COOLDOWN_NOW - timedelta(hours=25 + i)).isoformat(timespec="seconds"))

        # Insert a fresh gaming row.
        gaming_new = _scored_story_topic("FreshGaming", 90.0, topic="gaming")
        store.add_stories_to_store([gaming_new], [])

        picked = []
        async def fake_style(items, lm, **kw):
            picked.append(items[0].get("title"))
            return [{"title": "S", "body": "B"}]

        with patch("newsbot.jobs.llm_style_posts", new=fake_style), \
             patch("newsbot.jobs._build_lm_client", return_value=object()):
            os.environ.pop("BOT_TOKEN", None)
            os.environ.pop("NEWS_CHANNEL_ID", None)
            result = await coordinator.run_posting()

        assert result == 0
        assert picked == ["FreshGaming"], \
            "old posts (>24h) should not trigger cooldown"

    @pytest.mark.asyncio
    async def test_null_origin_topic_unaffected(self, coordinator, store, monkeypatch):
        """Rows with NULL/empty origin_topic are never excluded."""
        monkeypatch.setattr("newsbot.jobs.datetime", _frozen_dt(COOLDOWN_NOW))
        monkeypatch.setenv("NEWS_TOPIC_COOLDOWN_MAX", "1")

        # Insert a gaming post within 24h (cooldown=1 means 1 post blocks).
        s = _scored_story_topic("GamingPost", 100.0, topic="gaming")
        store.add_stories_to_store([s], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title=?", ("GamingPost",)
        ).fetchone()["id"]
        _mark_posted(store, rid, (COOLDOWN_NOW - timedelta(hours=1)).isoformat(timespec="seconds"))

        # Insert a NULL-topic row (hot) and a gaming row (also hot).
        null_row = _scored_story_topic("Null Topic Hot", 90.0, topic=None)
        gaming_row = _scored_story_topic("Gaming Hot", 80.0, topic="gaming")
        store.add_stories_to_store([null_row, gaming_row], [])

        picked = []
        async def fake_style(items, lm, **kw):
            picked.append(items[0].get("title"))
            return [{"title": "S", "body": "B"}]

        with patch("newsbot.jobs.llm_style_posts", new=fake_style), \
             patch("newsbot.jobs._build_lm_client", return_value=object()):
            os.environ.pop("BOT_TOKEN", None)
            os.environ.pop("NEWS_CHANNEL_ID", None)
            result = await coordinator.run_posting()

        assert result == 0
        # NULL-topic row must NOT be excluded (it's the hottest at 90).
        assert picked == ["Null Topic Hot"]

    @pytest.mark.asyncio
    async def test_cooldown_zero_disables(self, coordinator, store, monkeypatch):
        """NEWS_TOPIC_COOLDOWN_MAX=0 disables the filter."""
        monkeypatch.setattr("newsbot.jobs.datetime", _frozen_dt(COOLDOWN_NOW))
        monkeypatch.setenv("NEWS_TOPIC_COOLDOWN_MAX", "0")

        # Insert 5 gaming posts within 24h.
        for i in range(5):
            s = _scored_story_topic(f"Spam{i}", 100.0 - i, topic="gaming")
            store.add_stories_to_store([s], [])
            rid = store._conn.execute(
                "SELECT id FROM pending_posts WHERE title=?", (f"Spam{i}",)
            ).fetchone()["id"]
            _mark_posted(store, rid, (COOLDOWN_NOW - timedelta(hours=i + 1)).isoformat(timespec="seconds"))

        # Insert a fresh hot gaming row.
        fresh = _scored_story_topic("FreshGaming", 95.0, topic="gaming")
        store.add_stories_to_store([fresh], [])

        picked = []
        async def fake_style(items, lm, **kw):
            picked.append(items[0].get("title"))
            return [{"title": "S", "body": "B"}]

        with patch("newsbot.jobs.llm_style_posts", new=fake_style), \
             patch("newsbot.jobs._build_lm_client", return_value=object()):
            os.environ.pop("BOT_TOKEN", None)
            os.environ.pop("NEWS_CHANNEL_ID", None)
            result = await coordinator.run_posting()

        assert result == 0
        assert picked == ["FreshGaming"], \
            "cooldown=0 must not exclude any rows"


# --- AC 3: selection.py imports -------------------------------------------


class TestSelectionImports:
    """AC 3: selection.py still imports nothing beyond stdlib + newsbot.scoring."""

    def test_no_db_or_env_imports(self):
        import newsbot.selection as sel
        # Check imports are limited to stdlib + newsbot.scoring.
        tree = ast.parse(Path(sel.__file__).read_text())
        allowed_modules = {
            "statistics", "dataclasses", "datetime", "typing", "json",
            "newsbot.scoring", "__future__",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
                    assert mod in allowed_modules, \
                        f"selection.py imports forbidden module: {mod}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level == 0:  # absolute import
                    assert mod in allowed_modules, \
                        f"selection.py imports forbidden module: {mod}"


# --- AC 4: post_pick/post_skip log events carry cooldown-excluded count ----


class TestCooldownLogging:
    """AC 4: post_pick/post_skip log events carry the cooldown-excluded count."""

    @pytest.mark.asyncio
    async def test_post_pick_has_cooldown_excluded(self, coordinator, store, monkeypatch, caplog):
        """post_pick log event must include cooldown_excluded field."""
        monkeypatch.setattr("newsbot.jobs.datetime", _frozen_dt(COOLDOWN_NOW))
        monkeypatch.setenv("NEWS_TOPIC_COOLDOWN_MAX", "3")

        # 3 gaming posts within 24h.
        for i in range(3):
            s = _scored_story_topic(f"Posted{i}", 100.0, topic="gaming")
            store.add_stories_to_store([s], [])
            rid = store._conn.execute(
                "SELECT id FROM pending_posts WHERE title=?", (f"Posted{i}",)
            ).fetchone()["id"]
            _mark_posted(store, rid, (COOLDOWN_NOW - timedelta(hours=i + 1)).isoformat(timespec="seconds"))

        # A non-gaming row that will be picked.
        ai_row = _scored_story_topic("AI Story", 90.0, topic="ai")
        # A gaming row that will be excluded.
        gaming_row = _scored_story_topic("Gaming Excluded", 85.0, topic="gaming")
        store.add_stories_to_store([ai_row, gaming_row], [])

        async def fake_style(items, lm, **kw):
            return [{"title": "S", "body": "B"}]

        with patch("newsbot.jobs.llm_style_posts", new=fake_style), \
             patch("newsbot.jobs._build_lm_client", return_value=object()):
            os.environ.pop("BOT_TOKEN", None)
            os.environ.pop("NEWS_CHANNEL_ID", None)
            with caplog.at_level(logging.INFO, logger="newsbot.jobs"):
                await coordinator.run_posting()

        # Find the post_pick log event.
        pick_events = [
            json.loads(r.message) for r in caplog.records
            if r.message.startswith('{"event": "post_pick"')
        ]
        assert len(pick_events) >= 1, "post_pick event must be logged"
        assert "cooldown_excluded" in pick_events[0]
        assert pick_events[0]["cooldown_excluded"] >= 1, \
            "at least 1 gaming row should be cooldown-excluded"

    @pytest.mark.asyncio
    async def test_post_skip_has_cooldown_excluded(self, coordinator, store, monkeypatch, caplog):
        """post_skip log event must include cooldown_excluded field."""
        monkeypatch.setattr("newsbot.jobs.datetime", _frozen_dt(COOLDOWN_NOW))
        monkeypatch.setenv("NEWS_TOPIC_COOLDOWN_MAX", "1")

        # 1 gaming post within 24h (cooldown=1 means 1 blocks).
        s = _scored_story_topic("GamingPosted", 100.0, topic="gaming")
        store.add_stories_to_store([s], [])
        rid = store._conn.execute(
            "SELECT id FROM pending_posts WHERE title=?", ("GamingPosted",)
        ).fetchone()["id"]
        _mark_posted(store, rid, (COOLDOWN_NOW - timedelta(hours=1)).isoformat(timespec="seconds"))

        # Insert only a gaming row that will be excluded (below threshold after exclusion).
        # Actually we need it above threshold so the only reason it's not picked
        # is the exclusion. But with only 1 row excluded, pick_hottest returns
        # below_threshold since no eligible rows remain.
        gaming_hot = _scored_story_topic("Gaming Hot", 90.0, topic="gaming")
        store.add_stories_to_store([gaming_hot], [])

        with patch("newsbot.jobs.llm_style_posts", new=AsyncMock()), \
             patch("newsbot.jobs._build_lm_client", return_value=object()):
            os.environ.pop("BOT_TOKEN", None)
            os.environ.pop("NEWS_CHANNEL_ID", None)
            with caplog.at_level(logging.INFO, logger="newsbot.jobs"):
                await coordinator.run_posting()

        skip_events = [
            json.loads(r.message) for r in caplog.records
            if r.message.startswith('{"event": "post_skip"')
        ]
        assert len(skip_events) >= 1, "post_skip event must be logged"
        assert "cooldown_excluded" in skip_events[0]
        assert skip_events[0]["cooldown_excluded"] >= 1


# --- AC 5: README documents NEWS_TOPIC_COOLDOWN_MAX -----------------------


class TestReadmeDocuments:
    """AC 5: README env-var section documents NEWS_TOPIC_COOLDOWN_MAX."""

    def test_readme_has_cooldown_max(self):
        readme = Path(__file__).parent.parent / "README.md"
        text = readme.read_text()
        assert "NEWS_TOPIC_COOLDOWN_MAX" in text, \
            "README must document NEWS_TOPIC_COOLDOWN_MAX"
