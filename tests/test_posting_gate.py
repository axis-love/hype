"""T6: temperature-gated, style-at-pick posting in jobs.py.

_deliver_one picks the hottest eligible store row, styles it at pick time,
and delivers it. Result codes: 0 delivered, 1 failure (slot unconsumed),
3 empty store, 4 threshold skip (slot consumed).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from newsbot.db import NewsStore
from tests.helpers import coord_drain, coord_gen, coord_post, insert_story
from newsbot.jobs import JobCoordinator
from newsbot.outcome import Outcome


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    s = NewsStore(tmp_path / "gate.sqlite")
    yield s
    s.close()


@pytest.fixture
def settings():
    class MockSettings:
        def __init__(self):
            self._data = {}
        def get(self, section, key, default=None):
            return self._data.get(section, {}).get(key, default)
        def set(self, section, key, value):
            self._data.setdefault(section, {})[key] = value
    return MockSettings()


@pytest.fixture
def coordinator(store, settings) -> JobCoordinator:
    return JobCoordinator()


NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def _scored_story(title: str, engagement: float, *, merge_count: int = 1,
                  source_weight: float = 1.0) -> dict:
    """Story dict whose score_breakdown persists a known engagement score.

    With recency=1.0, weight as given, no bonuses, penalty 1.0 the row's
    current temperature at NOW equals engagement exactly.
    """
    published = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
    return {
        "title": title,
        "url": f"https://example.com/{title.lower().replace(' ', '-')}",
        "source": "hn",
        "source_name": "Hacker News",
        "snippet": f"Snippet for {title}",
        "upvotes": int(engagement),
        "comments": 1,
        "published_at": published,
        "score_breakdown": {
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
            "source_weight": source_weight,
            "topic_bonus": 0,
            "crosspost_bonus": 0.0,
            "matched_topics": [],
            "scored_at": published,
        },
    }


class TestPostingGate:
    @pytest.mark.asyncio
    async def test_hottest_picked_and_delivered(self, coordinator, store, settings, monkeypatch):
        """Hottest eligible row is styled and delivered; row marked posted."""
        monkeypatch.setattr("newsbot.poster.datetime", _frozen_dt(NOW))
        store.add_stories_to_store(
            [_scored_story("Cold story", 40.0), _scored_story("Hot story", 90.0)],
            [],
        )
        calls = []

        async def fake_style(items, lm, **kw):
            calls.append(items)
            return [{"title": "STYLED", "body": "Styled body"}]

        with patch("newsbot.poster.llm_style_posts", new=fake_style), \
             patch("newsbot.llm.build_lm_client", return_value=object()):
            result = await coord_post(coordinator, store, settings)

        assert result == Outcome.OK
        assert len(calls) == 1  # styled exactly one row, at pick time
        rows = store.list_store_rows("telegram")
        assert len(rows) == 1  # hot row posted, cold remains
        assert rows[0]["title"] == "Cold story"

    @pytest.mark.asyncio
    async def test_below_threshold_returns_4_nothing_posted(self, coordinator, store, settings, monkeypatch):
        """All rows below floor -> 4, nothing styled or posted."""
        monkeypatch.setattr("newsbot.poster.datetime", _frozen_dt(NOW))
        store.add_stories_to_store([_scored_story("Lukewarm", 10.0)], [])

        style_called = []

        async def fake_style(items, lm, **kw):
            style_called.append(1)
            return [{"title": "X", "body": "Y"}]

        with patch("newsbot.poster.llm_style_posts", new=fake_style), \
             patch("newsbot.poster.post_digest", new=AsyncMock()) as pd:
            result = await coord_post(coordinator, store, settings)

        assert result == Outcome.BELOW_THRESHOLD
        assert not style_called
        pd.assert_not_awaited()
        assert store.count_pending("telegram") == 1  # row still unposted

    @pytest.mark.asyncio
    async def test_empty_store_returns_3(self, coordinator, store, settings):
        assert await coord_post(coordinator, store, settings) == Outcome.NOTHING_TO_DO

    @pytest.mark.asyncio
    async def test_styler_failure_returns_1_row_stays_raw(self, coordinator, store, settings, monkeypatch):
        """Styler returns [] -> 1; row stays raw and unposted (retryable)."""
        monkeypatch.setattr("newsbot.poster.datetime", _frozen_dt(NOW))
        store.add_stories_to_store([_scored_story("Hot story", 90.0)], [])

        async def failing_style(items, lm, **kw):
            return []

        with patch("newsbot.poster.llm_style_posts", new=failing_style), \
             patch("newsbot.llm.build_lm_client", return_value=object()):
            result = await coord_post(coordinator, store, settings)

        assert result == Outcome.FAILED
        assert store.count_pending("telegram") == 1
        row = store.list_store_rows("telegram")[0]
        assert row["merge_count"] in (None, 1)  # untouched

    @pytest.mark.asyncio
    async def test_success_fills_styled_at_and_marks_posted(self, coordinator, store, settings, monkeypatch):
        """Successful delivery fills styled_at, marks posted."""
        monkeypatch.setattr("newsbot.poster.datetime", _frozen_dt(NOW))
        store.add_stories_to_store([_scored_story("Hot story", 90.0)], [])

        async def fake_style(items, lm, **kw):
            return [{"title": "STYLED", "body": "Styled body"}]

        posted = []

        async def fake_digest(message, **kw):
            posted.append(message)

        with patch("newsbot.poster.llm_style_posts", new=fake_style), \
             patch("newsbot.llm.build_lm_client", return_value=object()), \
             patch("newsbot.poster.post_digest", new=fake_digest), \
             patch.dict("os.environ", {"BOT_TOKEN": "t", "NEWS_CHANNEL_ID": "c"}):
            result = await coord_post(coordinator, store, settings)

        assert result == Outcome.OK
        assert posted and "STYLED" in posted[0]
        assert store.count_pending("telegram") == 0
        d = store._conn.execute(
            "SELECT styled_title, styled_body FROM deliveries WHERE channel='telegram'"
        ).fetchone()
        assert d["styled_title"] == "STYLED"
        assert d["styled_body"] == "Styled body"

    @pytest.mark.asyncio
    async def test_merge_multiplier_changes_ranking_not_eligibility(
        self, coordinator, store, settings, monkeypatch
    ):
        """Merge boost lifts a row's RANK only; a cold merged row stays gated."""
        monkeypatch.setattr("newsbot.poster.datetime", _frozen_dt(NOW))
        # Cold row (below floor) with many merges — multiplier must NOT
        # make it eligible.
        cold = _scored_story("Cold merged", 10.0)
        store.add_stories_to_store([cold], [])
        cold_id = store.list_store_rows("telegram")[0]["id"]
        store._conn.execute(
            "UPDATE pending_posts SET merge_count=5 WHERE id=?", (cold_id,)
        )
        store._conn.commit()
        # Hot row above floor, no merges.
        store.add_stories_to_store([_scored_story("Hot plain", 90.0)], [])

        picked = []

        async def fake_style(items, lm, **kw):
            picked.append(items[0].get("title"))
            return [{"title": "S", "body": "B"}]

        with patch("newsbot.poster.llm_style_posts", new=fake_style), \
             patch("newsbot.llm.build_lm_client", return_value=object()):
            result = await coord_post(coordinator, store, settings)

        assert result == Outcome.OK
        assert picked == ["Hot plain"]  # cold merged row never eligible

    @pytest.mark.asyncio
    async def test_merge_multiplier_breaks_ranking_tie(self, coordinator, store, settings, monkeypatch):
        """Equal temps: the merged row outranks the plain one (ranking only)."""
        monkeypatch.setattr("newsbot.poster.datetime", _frozen_dt(NOW))
        store.add_stories_to_store([_scored_story("Plain hot", 90.0)], [])
        merged = _scored_story("Merged hot", 90.0)
        store.add_stories_to_store([merged], [])
        merged_id = store.list_store_rows("telegram")[-1]["id"]
        store._conn.execute(
            "UPDATE pending_posts SET merge_count=3 WHERE id=?", (merged_id,)
        )
        store._conn.commit()

        picked = []

        async def fake_style(items, lm, **kw):
            picked.append(items[0].get("title"))
            return [{"title": "S", "body": "B"}]

        with patch("newsbot.poster.llm_style_posts", new=fake_style), \
             patch("newsbot.llm.build_lm_client", return_value=object()):
            result = await coord_post(coordinator, store, settings)

        assert result == Outcome.OK
        assert picked == ["Merged hot"]

    @pytest.mark.asyncio
    async def test_legacy_null_score_row_never_selected(self, coordinator, store, settings, monkeypatch):
        """Legacy row (engagement_score NULL) has temp 0.0 -> never eligible."""
        monkeypatch.setattr("newsbot.poster.datetime", _frozen_dt(NOW))
        store._conn.execute(
            "INSERT INTO pending_posts(title, url, created_at) VALUES(?,?,?)",
            ("Legacy", "https://legacy.example.com", NOW.isoformat()),
        )

        async def fake_style(items, lm, **kw):
            raise AssertionError("styler must not run for a below-threshold legacy row")

        with patch("newsbot.poster.llm_style_posts", new=fake_style):
            result = await coord_post(coordinator, store, settings)

        assert result == Outcome.BELOW_THRESHOLD
        assert store.count_pending("telegram") == 1

    @pytest.mark.asyncio
    async def test_drain_returns_0_on_threshold_skip(self, coordinator, store, settings, monkeypatch):
        """drain_posts treats 4 as terminal success (healthy 'nothing hot')."""
        monkeypatch.setattr("newsbot.poster.datetime", _frozen_dt(NOW))
        store.add_stories_to_store([_scored_story("Lukewarm", 10.0)], [])
        result = await coord_drain(coordinator, store, settings)
        assert result == Outcome.OK

    @pytest.mark.asyncio
    async def test_drain_stops_on_empty_after_posting(self, coordinator, store, settings, monkeypatch):
        """drain posts the hot row, then hits empty -> 0."""
        monkeypatch.setattr("newsbot.poster.datetime", _frozen_dt(NOW))
        store.add_stories_to_store([_scored_story("Hot story", 90.0)], [])

        async def fake_style(items, lm, **kw):
            return [{"title": "S", "body": "B"}]

        with patch("newsbot.poster.llm_style_posts", new=fake_style), \
             patch("newsbot.llm.build_lm_client", return_value=object()):
            result = await coord_drain(coordinator, store, settings)

        assert result == Outcome.OK
        assert store.count_pending("telegram") == 0


class _frozen_dt:
    """Stand-in for datetime whose now() returns a fixed instant."""

    def __init__(self, fixed: datetime):
        self._fixed = fixed

    def now(self, tz=None):
        return self._fixed if tz else self._fixed.replace(tzinfo=None)

    def __getattr__(self, name):
        return getattr(datetime, name)
