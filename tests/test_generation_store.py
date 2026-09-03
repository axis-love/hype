"""Additive raw-story generation pipeline (v2).

Digest fills the store with raw scored stories — no styling pass:
match→merge→append→evict, survivors marked seen, no styler anywhere.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest

from newsbot.db import NewsStore
from newsbot.main import _run_generation


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    s = NewsStore(tmp_path / "gen.sqlite")
    yield s
    s.close()


def _make_cfg(**over: Any) -> dict[str, Any]:
    """Minimal config dict accepted by _run_generation."""
    from newsbot.config import DEFAULT_SOURCE_WEIGHTS
    cfg = {
        "sources": {"hn": {"max_items": 5}},
        "source_weights": DEFAULT_SOURCE_WEIGHTS,
        "topic_boost": {},
        "lookback_hours": 48,
        "min_score": 0.0,
        "max_candidates": 10,
        "max_final_news": 5,
        "source_quota": 8,
        "llm_temperature": 0.4,
        "llm_max_tokens_filter": 800,
        "llm_max_tokens_digest": 8000,
        "style_prompt": "",
    }
    cfg.update(over)
    return cfg


def _story(title: str, url: str, upvotes: int = 100, hours_old: float = 2.0) -> dict:
    now = datetime.now(timezone.utc)
    published = (now - timedelta(hours=hours_old)).isoformat(timespec="seconds")
    return {
        "title": title,
        "url": url,
        "source": "hn",
        "source_name": "Hacker News",
        "snippet": f"Snippet for {title}",
        "upvotes": upvotes,
        "comments": upvotes // 10,
        "published_at": published,
    }


def _patch_pipeline(monkeypatch, cfg, stories, keep_ids):
    """Patch the collection/filter side of _run_generation deterministically."""
    monkeypatch.setattr("newsbot.main.load_config", lambda s: cfg)
    monkeypatch.setattr("newsbot.main._set_pre_merge_weights", lambda w: None)

    async def mock_collect_all(_cfg):
        return [dict(s) for s in stories]

    monkeypatch.setattr("newsbot.main.collect_all", mock_collect_all)
    monkeypatch.setattr("newsbot.main.filter_seen", lambda items, store: items)
    monkeypatch.setattr("newsbot.main.dedupe_and_merge", lambda items: items)

    # Real scoring runs here (deterministic, no network).

    # llm_filter is awaited; patch with an async function directly.
    async def mock_llm_filter(items, lm, **kw):
        return [
            {**dict(it), "short_summary": "llm summary", "category": "AI"}
            for it in items
            if it.get("candidate_id") in keep_ids
        ]

    monkeypatch.setattr("newsbot.main.llm_filter", mock_llm_filter)
    monkeypatch.setattr("newsbot.main.select_diverse_top_items", lambda items, n: items[:n])
    monkeypatch.setattr("newsbot.main._build_filter_lm_client", lambda: MagicMock())


class TestAdditiveGeneration:
    """(a) pre-seeded unposted rows survive a run."""

    async def test_preseeded_rows_survive(self, store, monkeypatch):
        seeded = _story("Seed Story", "https://seed.example.com/1", upvotes=50)
        seeded["score_breakdown"] = {
            "score": 50.0, "engagement": 40.0, "recency": 0.9,
            "source_weight": 1.0, "topic_bonus": 0, "crosspost_bonus": 0.0,
            "penalty": 1.0, "lookback_hours": 48, "published_at": seeded["published_at"],
        }
        inserted = store.add_stories_to_store([seeded], [])
        assert inserted == 1

        new_story = _story("Fresh Story", "https://fresh.example.com/2", upvotes=300)
        _patch_pipeline(monkeypatch, _make_cfg(), [new_story], keep_ids={"c001"})

        result = await _run_generation(store, MagicMock())
        assert result == 0

        rows = store.list_store_rows("telegram")
        titles = {r["title"] for r in rows}
        assert "Seed Story" in titles  # survived
        assert "Fresh Story" in titles  # appended

    async def test_duplicate_candidate_merges(self, store, monkeypatch):
        """(b) same story next cycle merges instead of inserting."""
        original = _story("The Big Launch", "https://news.example.com/launch", upvotes=200)
        original["score_breakdown"] = {
            "score": 200.0, "engagement": 180.0, "recency": 0.95,
            "source_weight": 1.0, "topic_bonus": 0, "crosspost_bonus": 0.0,
            "penalty": 1.0, "lookback_hours": 48, "published_at": original["published_at"],
            "upvotes": 200, "comments": 20,
        }
        store.add_stories_to_store([original], [])
        before = store.list_store_rows("telegram")
        assert len(before) == 1
        row_id = before[0]["id"]

        # Same URL returns next cycle (real world: filter_seen drops it, but the
        # merge path must handle it when a near-duplicate slips through via a
        # different URL/title identity).
        dup = _story("The Big Launch!", "https://news.example.com/launch?utm_source=x", upvotes=260)
        _patch_pipeline(monkeypatch, _make_cfg(), [dup], keep_ids={"c001"})

        result = await _run_generation(store, MagicMock())
        assert result == 0

        rows = store.list_store_rows("telegram")
        assert len(rows) == 1, "duplicate must merge, not insert a new row"
        row = next(r for r in rows if r["id"] == row_id)
        assert row["merge_count"] == 2
        assert row["upvotes"] == 260  # per-field max
        merged_urls = json.loads(row["merged_urls"] or "[]")
        assert "https://news.example.com/launch?utm_source=x" in merged_urls

    async def test_survivors_marked_seen(self, store, monkeypatch):
        """(c) ALL survivors (added + merged) are marked seen."""
        # Seed one row; the returning duplicate must be seen-marked even
        # though it merges (no new row inserted for it).
        original = _story("Seen Anchor", "https://anchor.example.com/1", upvotes=120)
        original["score_breakdown"] = {
            "score": 120.0, "engagement": 110.0, "recency": 0.9,
            "source_weight": 1.0, "topic_bonus": 0, "crosspost_bonus": 0.0,
            "penalty": 1.0, "lookback_hours": 48, "published_at": original["published_at"],
        }
        store.add_stories_to_store([original], [])

        stories = [
            _story("Story A", "https://a.example.com/1"),           # -> added
            _story("Seen Anchor", "https://anchor.example.com/1", upvotes=140),  # -> merged
        ]
        _patch_pipeline(monkeypatch, _make_cfg(), stories, keep_ids={"c001", "c002"})

        result = await _run_generation(store, MagicMock())
        assert result == 0
        assert store.is_seen("https://a.example.com/1", "Story A"), "added story must be seen"
        assert store.is_seen("https://anchor.example.com/1", "Seen Anchor"), \
            "merged story must also be seen (all survivors live in the store)"

    async def test_eviction_above_cap(self, store, monkeypatch):
        """(d) store above NEWS_STORE_CAP evicts coldest after insert."""
        # Seed cap-many rows (all hotter or colder — make seeds cold via old published_at).
        cap = 3
        monkeypatch.setenv("NEWS_STORE_CAP", str(cap))
        for i in range(cap):
            old = _story(f"Old {i}", f"https://old.example.com/{i}", upvotes=5, hours_old=46)
            old["score_breakdown"] = {
                "score": 5.0, "engagement": 4.0, "recency": 0.2,
                "source_weight": 1.0, "topic_bonus": 0, "crosspost_bonus": 0.0,
                "penalty": 1.0, "lookback_hours": 48, "published_at": old["published_at"],
            }
            store.add_stories_to_store([old], [])

        fresh = _story("Hot Fresh", "https://fresh.example.com/x", upvotes=900, hours_old=0.5)
        _patch_pipeline(monkeypatch, _make_cfg(), [fresh], keep_ids={"c001"})

        result = await _run_generation(store, MagicMock())
        assert result == 0

        rows = store.list_store_rows("telegram")
        assert len(rows) == cap, f"store must be capped at {cap}, got {len(rows)}"
        titles = {r["title"] for r in rows}
        assert "Hot Fresh" in titles, "the hot new row must survive eviction"

    async def test_no_styler_in_generation(self, monkeypatch):
        """(e) llm_style_posts / _build_lm_client never called during generation."""
        import newsbot.main as m

        style_called = {"n": 0}

        async def exploding_style(*a, **kw):
            style_called["n"] += 1
            raise AssertionError("styler must not run in generation")

        monkeypatch.setattr(m, "llm_style_posts", exploding_style)

        def exploding_client():
            raise AssertionError("_build_lm_client must not run in generation")

        monkeypatch.setattr(m, "_build_lm_client", exploding_client)

        store = MagicMock()
        store.is_seen_batch.return_value = set()
        store.list_store_rows.return_value = []
        store.evict_coldest.return_value = 0

        stories = [_story("Only", "https://only.example.com/1")]
        _patch_pipeline(monkeypatch, _make_cfg(), stories, keep_ids={"c001"})

        result = await _run_generation(store, MagicMock())
        assert result == 0
        assert style_called["n"] == 0

    async def test_merges_only_still_success(self, store, monkeypatch):
        """Empty to_add with non-empty merges -> return 0 (not 3)."""
        original = _story("Recurring", "https://rec.example.com/1", upvotes=100)
        original["score_breakdown"] = {
            "score": 100.0, "engagement": 90.0, "recency": 0.9,
            "source_weight": 1.0, "topic_bonus": 0, "crosspost_bonus": 0.0,
            "penalty": 1.0, "lookback_hours": 48, "published_at": original["published_at"],
        }
        store.add_stories_to_store([original], [])

        dup = _story("Recurring (again)", "https://rec.example.com/1", upvotes=150)
        _patch_pipeline(monkeypatch, _make_cfg(), [dup], keep_ids={"c001"})

        result = await _run_generation(store, MagicMock())
        assert result == 0
        rows = store.list_store_rows("telegram")
        assert len(rows) == 1 and rows[0]["merge_count"] == 2


# --- OQ-4: pipeline extraction + dry-run write-freedom -------------------

class TestRunGenerationPipeline:
    """_run_generation_pipeline: pure pipeline, no DB writes."""

    async def test_funnel_counts_correct(self, store, monkeypatch):
        """Funnel: collected → unseen → deduped → above_min → filter → kept → final."""
        from newsbot.main import _run_generation_pipeline

        stories = [
            _story("Story A", "https://a.example.com/1", upvotes=200),
            _story("Story B", "https://b.example.com/2", upvotes=150),
            _story("Story C", "https://c.example.com/3", upvotes=50),
        ]
        _patch_pipeline(monkeypatch, _make_cfg(), stories, keep_ids={"c001", "c002"})

        result = await _run_generation_pipeline(store, _make_cfg())
        assert result is not None
        assert result.collected == 3
        assert result.unseen == 3   # filter_seen mocked to identity
        assert result.deduped == 3  # dedupe mocked to identity
        assert result.above_min_score == 3  # all above min_score=0
        assert result.sent_to_filter == 3
        assert result.llm_kept == 2  # only c001 and c002 kept by mock LLM
        assert result.final_count == 2

    async def test_items_classified_add_vs_merge(self, store, monkeypatch):
        """Items that match an existing store row are 'merge', others 'add'."""
        from newsbot.main import _run_generation_pipeline

        # Pre-seed a row that will match one of the candidates.
        original = _story("Existing", "https://existing.example.com/1", upvotes=100)
        original["score_breakdown"] = {
            "score": 100.0, "engagement": 90.0, "recency": 0.9,
            "source_weight": 1.0, "topic_bonus": 0, "crosspost_bonus": 0.0,
            "penalty": 1.0, "lookback_hours": 48, "published_at": original["published_at"],
        }
        store.add_stories_to_store([original], [])
        row_id = store.list_store_rows("telegram")[0]["id"]

        stories = [
            _story("New Story", "https://new.example.com/2", upvotes=200),
            _story("Existing!", "https://existing.example.com/1", upvotes=250),
        ]
        _patch_pipeline(monkeypatch, _make_cfg(), stories, keep_ids={"c001", "c002"})

        result = await _run_generation_pipeline(store, _make_cfg())
        assert result is not None
        actions = {item["title"]: item["action"] for item in result.items}
        assert actions["New Story"] == "add"
        assert actions["Existing!"] == "merge"
        merge_item = next(i for i in result.items if i["action"] == "merge")
        assert merge_item["merge_row_id"] == row_id

    async def test_returns_none_on_empty_collection(self, store, monkeypatch):
        from newsbot.main import _run_generation_pipeline

        _patch_pipeline(monkeypatch, _make_cfg(), [], keep_ids=set())
        result = await _run_generation_pipeline(store, _make_cfg())
        assert result is None

    async def test_returns_none_when_llm_keeps_zero(self, store, monkeypatch):
        from newsbot.main import _run_generation_pipeline

        stories = [_story("Story A", "https://a.example.com/1")]
        _patch_pipeline(monkeypatch, _make_cfg(), stories, keep_ids=set())
        result = await _run_generation_pipeline(store, _make_cfg())
        assert result is None

    async def test_no_db_writes(self, store, monkeypatch):
        """Pipeline must not write to the store — row count and seen unchanged."""
        from newsbot.main import _run_generation_pipeline

        # Pre-seed one row so we can verify it's untouched.
        original = _story("Seed", "https://seed.example.com/1", upvotes=100)
        original["score_breakdown"] = {
            "score": 100.0, "engagement": 90.0, "recency": 0.9,
            "source_weight": 1.0, "topic_bonus": 0, "crosspost_bonus": 0.0,
            "penalty": 1.0, "lookback_hours": 48, "published_at": original["published_at"],
        }
        store.add_stories_to_store([original], [])

        rows_before = len(store.list_store_rows("telegram"))
        seen_before = store._conn.execute("SELECT COUNT(*) AS n FROM seen").fetchone()["n"]

        stories = [
            _story("Fresh", "https://fresh.example.com/2", upvotes=300),
            _story("Seed!", "https://seed.example.com/1", upvotes=350),
        ]
        _patch_pipeline(monkeypatch, _make_cfg(), stories, keep_ids={"c001", "c002"})

        result = await _run_generation_pipeline(store, _make_cfg())
        assert result is not None

        rows_after = len(store.list_store_rows("telegram"))
        seen_after = store._conn.execute("SELECT COUNT(*) AS n FROM seen").fetchone()["n"]
        assert rows_after == rows_before, "pipeline must not add rows"
        assert seen_after == seen_before, "pipeline must not mark seen"
        # merge_count unchanged — no merge written.
        row = store.list_store_rows("telegram")[0]
        assert row["merge_count"] == 1


class TestFormatDryRunReport:
    """_format_dry_run_report renders funnel + per-item classification."""

    def test_renders_funnel_and_items(self):
        from newsbot.main import _format_dry_run_report, GenerationPipelineResult

        result = GenerationPipelineResult(
            collected=74, unseen=41, deduped=33, above_min_score=20,
            sent_to_filter=15, llm_kept=12, final_count=8,
            items=[
                {"title": "Hot Story", "source": "hn", "score": 250.0,
                 "action": "add", "merge_row_id": None,
                 "category": "AI", "importance": 5},
                {"title": "Merged Tale", "source": "reddit", "score": 180.0,
                 "action": "merge", "merge_row_id": 42,
                 "category": "", "importance": ""},
            ],
            failed_collectors=[],
        )
        report = _format_dry_run_report(result)
        assert "collected 74" in report
        assert "unseen 41" in report
        assert "final 8" in report
        assert "Hot Story" in report
        assert "ADD" in report
        assert "MERGE" in report
        assert "row 42" in report

    def test_failed_collectors_shown(self):
        from newsbot.main import _format_dry_run_report, GenerationPipelineResult

        result = GenerationPipelineResult(
            collected=10, unseen=10, deduped=10, above_min_score=5,
            sent_to_filter=5, llm_kept=3, final_count=2,
            items=[],
            failed_collectors=["reddit", "github"],
        )
        report = _format_dry_run_report(result)
        assert "reddit" in report
        assert "github" in report

    def test_empty_items_just_funnel(self):
        from newsbot.main import _format_dry_run_report, GenerationPipelineResult

        result = GenerationPipelineResult(
            collected=0, unseen=0, deduped=0, above_min_score=0,
            sent_to_filter=0, llm_kept=0, final_count=0,
            items=[],
            failed_collectors=[],
        )
        report = _format_dry_run_report(result)
        assert "collected 0" in report
        assert "final 0" in report

    def test_dry_run_report_has_no_fenced_markdown_when_empty(self):
        """The base report has no fenced markdown block when items is empty."""
        from newsbot.main import _format_dry_run_report, GenerationPipelineResult

        result = GenerationPipelineResult(
            collected=5, unseen=5, deduped=5, above_min_score=3,
            sent_to_filter=3, llm_kept=2, final_count=0,
            items=[],
            failed_collectors=[],
        )
        report = _format_dry_run_report(result)
        assert "```markdown" not in report
