"""Additive raw-story store primitives — migration 4 (flow_001092, TDD RED)."""
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from newsbot.db import NewsStore
from newsbot.scoring import engagement


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    s = NewsStore(tmp_path / "test.sqlite")
    yield s
    s.close()


def _bd(**overrides) -> dict:
    """A complete score_breakdown dict with sane defaults; override per test."""
    base = {
        "score": 120.0,
        "engagement": 100.0,
        "recency": 0.9,
        "source_weight": 1.2,
        "topic_bonus": 10,
        "crosspost_bonus": 0.0,
        "penalty": 1.0,
        "matched_topics": ["ai"],
        "origin_topic": "ai",
        "scored_at": "2026-08-16T05:00:00+00:00",
        "lookback_hours": 48.0,
        "source": "hn",
        "published_at": "2026-08-16T03:00:00+00:00",
        "upvotes": 100,
        "comments": 10,
        "stars": 5,
        "reposts": 2,
        "crosspost_count": 1,
    }
    base.update(overrides)
    return base


def _story(title="Story A", url="https://a.example.com/1", **bd_overrides) -> dict:
    return {
        "title": title,
        "url": url,
        "category": "AI",
        "snippet": "A snippet.",
        "source_name": "Hacker News",
        "source": "hn",
        "raw_json": {"payload": "x"},
        "score_breakdown": _bd(**bd_overrides),
    }


def _insert_legacy(store: NewsStore, title: str, url: str) -> int:
    """Insert a row the pre-v2 way (styled, no score data) via add_pending_post."""
    row_id = store.add_pending_post({"title": title, "body": f"body of {title}", "url": url})
    assert row_id is not None
    return row_id


# --- Migration 4 ----------------------------------------------------------


class TestMigration4:
    def test_new_columns_exist(self, store):
        cols = {c["name"] for c in store._conn.execute("PRAGMA table_info(pending_posts)")}
        for col in ("merge_count", "merged_urls", "snippet", "source_name", "raw_json", "styled_at"):
            assert col in cols, f"missing column {col}"

    def test_daily_summaries_table_exists(self, store):
        row = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_summaries'"
        ).fetchone()
        assert row is not None

    def test_merge_count_defaults_to_1_for_legacy_rows(self, store):
        _insert_legacy(store, "Legacy", "http://legacy.example.com")
        row = store._conn.execute(
            "SELECT merge_count, merged_urls, styled_at FROM pending_posts"
        ).fetchone()
        assert row["merge_count"] == 1
        assert row["merged_urls"] is None
        assert row["styled_at"] is None

    def test_migration_recorded(self, store):
        row = store._conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert row["version"] == 7


# --- add_stories_to_store -------------------------------------------------


class TestAddStoriesToStore:
    def test_additive_insert_preserves_existing_unposted_rows(self, store):
        _insert_legacy(store, "Old1", "http://old1.example.com")
        _insert_legacy(store, "Old2", "http://old2.example.com")

        inserted = store.add_stories_to_store([_story()], [])
        assert inserted == 1
        rows = store.list_store_rows()
        titles = {r["title"] for r in rows}
        assert titles == {"Old1", "Old2", "Story A"}

    def test_raw_insert_has_empty_body_and_null_styled_at(self, store):
        store.add_stories_to_store([_story()], [])
        row = store._conn.execute("SELECT body, styled_at FROM pending_posts").fetchone()
        assert row["body"] == ""
        assert row["styled_at"] is None

    def test_stores_raw_material_and_score_columns(self, store):
        bd = _bd()
        store.add_stories_to_store([_story()], [])
        row = dict(store._conn.execute("SELECT * FROM pending_posts").fetchone())
        assert row["snippet"] == "A snippet."
        assert row["source_name"] == "Hacker News"
        assert json.loads(row["raw_json"]) == {"payload": "x"}
        assert row["merge_count"] == 1
        # Every score-component column persisted from score_breakdown.
        assert row["score_at_queue"] == bd["score"]
        assert row["engagement_score"] == bd["engagement"]
        assert row["recency_at_queue"] == bd["recency"]
        assert row["source_weight"] == bd["source_weight"]
        assert row["topic_bonus"] == bd["topic_bonus"]
        assert row["crosspost_bonus"] == bd["crosspost_bonus"]
        assert row["penalty"] == bd["penalty"]
        assert row["lookback_hours"] == bd["lookback_hours"]
        assert row["source"] == bd["source"]
        assert row["published_at"] == bd["published_at"]
        assert row["upvotes"] == bd["upvotes"]
        assert row["comments"] == bd["comments"]
        assert row["stars"] == bd["stars"]
        assert row["reposts"] == bd["reposts"]
        assert row["crosspost_count"] == bd["crosspost_count"]
        assert json.loads(row["matched_topics"]) == ["ai"]
        assert row["origin_topic"] == bd["origin_topic"]
        assert row["scored_at"] == bd["scored_at"]

    def test_raw_json_string_passthrough(self, store):
        story = _story()
        story["raw_json"] = '{"already": "serialized"}'
        store.add_stories_to_store([story], [])
        row = store._conn.execute("SELECT raw_json FROM pending_posts").fetchone()
        assert row["raw_json"] == '{"already": "serialized"}'

    def test_marks_seen_items(self, store):
        story = _story()
        seen = [{"url": story["url"], "title": story["title"]}]
        store.add_stories_to_store([story], seen)
        assert store.is_seen(story["url"], story["title"])

    def test_seen_marking_atomic_with_inserts(self, store):
        """If the transaction fails, neither rows nor seen entries persist."""
        import sqlite3

        class FailingCursor:
            def execute(self, sql, *args):
                raise sqlite3.OperationalError("disk full")

            def close(self):
                pass

        class FailingConn:
            def cursor(self):
                return FailingCursor()

        original = store._conn
        store._conn = FailingConn()  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError):
                store.add_stories_to_store(
                    [_story()], [{"url": "http://s.example.com", "title": "S"}]
                )
        finally:
            store._conn = original
        assert not store.is_seen("http://s.example.com", "S")
        assert store.count_pending() == 0

    def test_returns_inserted_count(self, store):
        stories = [_story(title=f"S{i}", url=f"https://a.example.com/{i}") for i in range(4)]
        assert store.add_stories_to_store(stories, []) == 4


# --- list_store_rows ------------------------------------------------------


class TestListStoreRows:
    def test_excludes_posted_rows(self, store):
        rid = _insert_legacy(store, "Posted", "http://p.example.com")
        store.mark_posted(rid)
        store.add_stories_to_store([_story()], [])
        rows = store.list_store_rows()
        assert [r["title"] for r in rows] == ["Story A"]

    def test_includes_required_fields(self, store):
        store.add_stories_to_store([_story()], [])
        row = store.list_store_rows()[0]
        for key in (
            "id", "title", "url", "snippet", "source_name", "raw_json", "category",
            "source", "published_at", "upvotes", "comments", "stars", "reposts",
            "crosspost_count", "penalty", "lookback_hours", "score_at_queue",
            "engagement_score", "recency_at_queue", "source_weight", "topic_bonus",
            "crosspost_bonus", "matched_topics", "scored_at", "merge_count", "merged_urls",
        ):
            assert key in row, f"missing field {key}"


# --- merge_into_store_row -------------------------------------------------


class TestMergeIntoStoreRow:
    def _seed_scored_row(self, store, **bd_overrides) -> int:
        store.add_stories_to_store([_story(**bd_overrides)], [])
        return store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]

    def test_merge_increments_merge_count(self, store):
        rid = self._seed_scored_row(store)
        candidate = _story(upvotes=5, comments=1, stars=0, reposts=0)
        store.merge_into_store_row(rid, candidate, "https://b.example.com/1")
        row = store._conn.execute("SELECT merge_count FROM pending_posts WHERE id=?", (rid,)).fetchone()
        assert row["merge_count"] == 2

    def test_merge_takes_per_field_max(self, store):
        # Stored: upvotes 100, comments 10, stars 5, reposts 2.
        rid = self._seed_scored_row(store, upvotes=100, comments=10, stars=5, reposts=2)
        # Candidate wins on comments+stars, loses on upvotes+reposts.
        candidate = _story(upvotes=50, comments=99, stars=8, reposts=0)
        store.merge_into_store_row(rid, candidate, "https://b.example.com/1")
        row = store._conn.execute("SELECT * FROM pending_posts WHERE id=?", (rid,)).fetchone()
        assert row["upvotes"] == 100
        assert row["comments"] == 99
        assert row["stars"] == 8
        assert row["reposts"] == 2

    def test_merge_published_at_takes_max(self, store):
        rid = self._seed_scored_row(store, published_at="2026-08-15T00:00:00+00:00")
        candidate = _story(published_at="2026-08-16T06:00:00+00:00")
        store.merge_into_store_row(rid, candidate, "https://b.example.com/1")
        row = store._conn.execute("SELECT published_at FROM pending_posts WHERE id=?", (rid,)).fetchone()
        assert row["published_at"] == "2026-08-16T06:00:00+00:00"

    def test_merge_published_at_keeps_stored_when_newer(self, store):
        rid = self._seed_scored_row(store, published_at="2026-08-16T06:00:00+00:00")
        candidate = _story(published_at="2026-08-15T00:00:00+00:00")
        store.merge_into_store_row(rid, candidate, "https://b.example.com/1")
        row = store._conn.execute("SELECT published_at FROM pending_posts WHERE id=?", (rid,)).fetchone()
        assert row["published_at"] == "2026-08-16T06:00:00+00:00"

    def test_regression_hotter_stored_row_engagement_does_not_drop(self, store):
        """Stored row hotter than candidate: engagement_score must NOT drop.

        Recomputing from per-field maxima guarantees monotonicity — copying
        the candidate's engagement (v1 plan draft) would have lowered it.
        """
        stored_eng = engagement({"upvotes": 500, "comments": 80, "stars": 30, "reposts": 10})
        rid = self._seed_scored_row(
            store, upvotes=500, comments=80, stars=30, reposts=10, engagement=stored_eng
        )
        # Candidate is much colder on every field.
        candidate = _story(upvotes=1, comments=0, stars=0, reposts=0)
        assert engagement(candidate) < stored_eng
        store.merge_into_store_row(rid, candidate, "https://b.example.com/1")
        row = store._conn.execute("SELECT engagement_score FROM pending_posts WHERE id=?", (rid,)).fetchone()
        assert row["engagement_score"] >= stored_eng

    def test_merge_recomputes_engagement_from_merged_fields(self, store):
        rid = self._seed_scored_row(store, upvotes=100, comments=10, stars=5, reposts=2)
        candidate = _story(upvotes=50, comments=99, stars=8, reposts=0)
        store.merge_into_store_row(rid, candidate, "https://b.example.com/1")
        row = store._conn.execute("SELECT * FROM pending_posts WHERE id=?", (rid,)).fetchone()
        expected = engagement({
            "upvotes": row["upvotes"], "comments": row["comments"],
            "stars": row["stars"], "reposts": row["reposts"],
        })
        assert row["engagement_score"] == pytest.approx(expected)

    def test_merged_urls_dedup(self, store):
        rid = self._seed_scored_row(store)
        url = "https://b.example.com/1"
        store.merge_into_store_row(rid, _story(), url)
        store.merge_into_store_row(rid, _story(), url)  # same URL again
        row = store._conn.execute("SELECT merged_urls FROM pending_posts WHERE id=?", (rid,)).fetchone()
        assert json.loads(row["merged_urls"]) == [url]

    def test_merged_urls_appends_distinct(self, store):
        rid = self._seed_scored_row(store)
        store.merge_into_store_row(rid, _story(), "https://b.example.com/1")
        store.merge_into_store_row(rid, _story(), "https://c.example.com/2")
        row = store._conn.execute("SELECT merged_urls FROM pending_posts WHERE id=?", (rid,)).fetchone()
        assert json.loads(row["merged_urls"]) == ["https://b.example.com/1", "https://c.example.com/2"]

    def test_merge_fills_null_origin_topic_from_candidate(self, store):
        """Stored row with NULL origin_topic picks up the candidate's pack."""
        rid = self._seed_scored_row(store, origin_topic=None)
        candidate = _story(origin_topic="gaming")
        store.merge_into_store_row(rid, candidate, "https://b.example.com/1")
        row = store._conn.execute("SELECT origin_topic FROM pending_posts WHERE id=?", (rid,)).fetchone()
        assert row["origin_topic"] == "gaming"

    def test_merge_keeps_stored_origin_topic_when_set(self, store):
        """Stored origin_topic wins — the pack of first surfacing is preserved."""
        rid = self._seed_scored_row(store, origin_topic="ai")
        candidate = _story(origin_topic="gaming")
        store.merge_into_store_row(rid, candidate, "https://b.example.com/1")
        row = store._conn.execute("SELECT origin_topic FROM pending_posts WHERE id=?", (rid,)).fetchone()
        assert row["origin_topic"] == "ai"

    def test_merge_refreshes_components_from_candidate_breakdown(self, store):
        rid = self._seed_scored_row(
            store, source_weight=1.0, topic_bonus=0, crosspost_bonus=0.0,
            penalty=1.0, lookback_hours=48.0,
        )
        candidate = _story(source_weight=1.5, topic_bonus=25, crosspost_bonus=30.0,
                           penalty=0.8, lookback_hours=72.0)
        store.merge_into_store_row(rid, candidate, "https://b.example.com/1")
        row = store._conn.execute("SELECT * FROM pending_posts WHERE id=?", (rid,)).fetchone()
        assert row["source_weight"] == 1.5
        assert row["topic_bonus"] == 25
        assert row["crosspost_bonus"] == 30.0
        assert row["penalty"] == 0.8
        assert row["lookback_hours"] == 72.0

    def test_merge_recomputes_score_at_queue_from_rebuilt_components(self, store):
        rid = self._seed_scored_row(
            store, upvotes=100, comments=10, stars=5, reposts=2,
            recency=0.9, source_weight=1.0, topic_bonus=0, crosspost_bonus=0.0, penalty=1.0,
        )
        candidate = _story(source_weight=1.2, topic_bonus=20, crosspost_bonus=30.0,
                           penalty=1.0, recency=0.9)
        store.merge_into_store_row(rid, candidate, "https://b.example.com/1")
        row = store._conn.execute("SELECT * FROM pending_posts WHERE id=?", (rid,)).fetchone()
        merged_eng = engagement({
            "upvotes": row["upvotes"], "comments": row["comments"],
            "stars": row["stars"], "reposts": row["reposts"],
        })
        expected = (
            merged_eng * row["recency_at_queue"] * row["source_weight"]
            + row["topic_bonus"] + row["crosspost_bonus"]
        ) * row["penalty"]
        assert row["score_at_queue"] == pytest.approx(expected)


# --- set_styled_content ---------------------------------------------------


class TestSetStyledContent:
    def test_fills_body_title_and_styled_at(self, store):
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]
        store.set_styled_content(rid, "Styled Title", "Styled body.")
        row = store._conn.execute("SELECT * FROM pending_posts WHERE id=?", (rid,)).fetchone()
        assert row["title"] == "Styled Title"
        assert row["body"] == "Styled body."
        assert row["styled_at"] is not None
        # styled_at must be valid UTC ISO-8601.
        parsed = datetime.fromisoformat(row["styled_at"])
        assert parsed.tzinfo is not None

    def test_raw_marker_cleared_after_styling(self, store):
        store.add_stories_to_store([_story()], [])
        rid = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]
        store.set_styled_content(rid, "T", "B")
        row = store._conn.execute(
            "SELECT 1 FROM pending_posts WHERE body='' AND styled_at IS NULL"
        ).fetchone()
        assert row is None


# --- evict_coldest --------------------------------------------------------


class TestEvictColdest:
    def test_removes_only_coldest_until_cap(self, store):
        for i in range(5):
            store.add_stories_to_store([_story(title=f"S{i}", url=f"https://a.example.com/{i}")], [])
        rows = store.list_store_rows()
        # Row i gets temperature i*10 → S0 coldest, S4 hottest.
        temps = {r["id"]: float(i * 10) for i, r in enumerate(rows)}
        evicted = store.evict_coldest(temps, cap=3)
        assert evicted == 2
        remaining = {r["title"] for r in store.list_store_rows()}
        assert remaining == {"S2", "S3", "S4"}

    def test_never_touches_posted_rows(self, store):
        rid = _insert_legacy(store, "Posted", "http://p.example.com")
        store.mark_posted(rid)
        store.add_stories_to_store(
            [_story(title=f"S{i}", url=f"https://a.example.com/{i}") for i in range(3)], []
        )
        rows = store.list_store_rows()
        temps = {r["id"]: 1.0 for r in rows}
        temps[rid] = 0.0  # posted row would be "coldest" — must survive
        evicted = store.evict_coldest(temps, cap=2)
        assert evicted == 1
        posted = store._conn.execute(
            "SELECT 1 FROM pending_posts WHERE id=?", (rid,)
        ).fetchone()
        assert posted is not None

    def test_noop_when_under_cap(self, store):
        store.add_stories_to_store([_story()], [])
        rows = store.list_store_rows()
        assert store.evict_coldest({rows[0]["id"]: 5.0}, cap=5) == 0
        assert len(store.list_store_rows()) == 1

    def test_ignores_temps_for_unknown_ids(self, store):
        store.add_stories_to_store([_story()], [])
        evicted = store.evict_coldest({99999: 0.0}, cap=0)
        assert evicted == 1


# --- list_posted_since ----------------------------------------------------


class TestListPostedSince:
    def test_boundary_inclusive(self, store):
        rid = _insert_legacy(store, "AtBoundary", "http://b.example.com")
        boundary = "2026-08-16T12:00:00+00:00"
        store.mark_delivered(rid, "telegram", message_id=None)
        # Override the delivered_at to the boundary timestamp.
        store._conn.execute(
            "UPDATE deliveries SET delivered_at=? WHERE post_id=? AND channel='telegram'",
            (boundary, rid),
        )
        rows = store.list_posted_since("telegram", boundary)
        assert [r["title"] for r in rows] == ["AtBoundary"]

    def test_excludes_older_and_unposted(self, store):
        rid_old = _insert_legacy(store, "Old", "http://old.example.com")
        _insert_legacy(store, "Unposted", "http://u.example.com")
        rid_new = _insert_legacy(store, "New", "http://new.example.com")
        store.mark_delivered(rid_old, "telegram")
        store._conn.execute(
            "UPDATE deliveries SET delivered_at=? WHERE post_id=? AND channel='telegram'",
            ("2026-08-15T00:00:00+00:00", rid_old),
        )
        store.mark_delivered(rid_new, "telegram")
        store._conn.execute(
            "UPDATE deliveries SET delivered_at=? WHERE post_id=? AND channel='telegram'",
            ("2026-08-16T13:00:00+00:00", rid_new),
        )
        rows = store.list_posted_since("telegram", "2026-08-16T00:00:00+00:00")
        assert [r["title"] for r in rows] == ["New"]

    def test_ordered_by_posted_at_asc(self, store):
        r1 = _insert_legacy(store, "First", "http://1.example.com")
        r2 = _insert_legacy(store, "Second", "http://2.example.com")
        store.mark_delivered(r2, "telegram")
        store._conn.execute(
            "UPDATE deliveries SET delivered_at=? WHERE post_id=? AND channel='telegram'",
            ("2026-08-16T10:00:00+00:00", r2),
        )
        store.mark_delivered(r1, "telegram")
        store._conn.execute(
            "UPDATE deliveries SET delivered_at=? WHERE post_id=? AND channel='telegram'",
            ("2026-08-16T09:00:00+00:00", r1),
        )
        rows = store.list_posted_since("telegram", "2026-08-16T00:00:00+00:00")
        assert [r["title"] for r in rows] == ["First", "Second"]


# --- daily summaries ------------------------------------------------------


class TestSummaries:
    def test_add_and_get_summary(self, store):
        store.add_summary("2026-08-16", "A recap.", "test-model", 7)
        got = store.get_summary_for_day("2026-08-16")
        assert got is not None
        assert got["day"] == "2026-08-16"
        assert got["summary_text"] == "A recap."
        assert got["model_used"] == "test-model"
        assert got["item_count"] == 7
        assert got["posted_at"]

    def test_get_summary_missing_day_returns_none(self, store):
        assert store.get_summary_for_day("1999-01-01") is None

    def test_day_unique_constraint(self, store):
        import sqlite3
        store.add_summary("2026-08-16", "first", "m", 1)
        with pytest.raises(sqlite3.IntegrityError):
            store.add_summary("2026-08-16", "second", "m", 1)


# --- deleted legacy methods -----------------------------------------------


class TestDeletedLegacyMethods:
    def test_replace_unposted_batch_removed(self, store):
        assert not hasattr(store, "replace_unposted_batch")

    def test_get_next_pending_post_removed(self, store):
        assert not hasattr(store, "get_next_pending_post")
