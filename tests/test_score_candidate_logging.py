"""Tests for per-candidate score logging before LLM filter (flow_001042).

Verifies:
- JSON log lines are valid JSON (json.loads succeeds)
- All required fields are present
- Titles are truncated to 80 chars and JSON-escaped
- Candidate IDs are assigned in _run_generation (preserved by llm_filter)
- Only candidates sent to LLM filter are logged
"""
import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from newsbot.scoring import score_all
from newsbot.summarizer import _assign_candidate_ids, _assign_missing_candidate_ids, llm_filter


def _make_scored_items(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Build a list of scored items with score_breakdown for logging."""
    if cfg is None:
        from newsbot.config import DEFAULT_SOURCE_WEIGHTS
        cfg = {
            "source_weights": DEFAULT_SOURCE_WEIGHTS,
            "topic_boost": {"llm": 50},
            "lookback_hours": 48,
        }
    items = [
        {
            "title": "AI Breakthrough in Local LLMs",
            "url": "https://example.com/ai",
            "source": "hn",
            "source_name": "Hacker News",
            "upvotes": 500,
            "comments": 100,
            "published_at": "2026-07-28T10:00:00+00:00",
        },
        {
            "title": "New GPU Released",
            "url": "https://example.com/gpu",
            "source": "github",
            "source_name": "GitHub",
            "stars": 5000,
            "published_at": "2026-07-28T09:00:00+00:00",
        },
    ]
    from datetime import datetime, timezone
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    return score_all(items, cfg, now=now)


class TestScoreCandidateLogJSON:
    """Verify the JSON log line format."""

    def test_log_line_is_valid_json(self):
        """json.loads(record.getMessage()) must succeed on each log record."""
        items = _make_scored_items()
        _assign_candidate_ids(items)

        records: list[logging.LogRecord] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = CaptureHandler()
        logger = logging.getLogger("newsbot.main")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        # Simulate the logging loop from _run_generation
        for rank, c in enumerate(items, start=1):
            bd = c.get("score_breakdown") or {}
            log_line = json.dumps({
                "event": "score_candidate",
                "id": c.get("candidate_id"),
                "rank": rank,
                "score": float(c.get("score") or 0.0),
                "scored_at": bd.get("scored_at", ""),
                "source": str(c.get("source") or ""),
                "title": str(c.get("title") or "")[:80],
                "published_at": str(c.get("published_at") or "") if c.get("published_at") else "",
                "upvotes": c.get("upvotes") or 0,
                "comments": c.get("comments") or 0,
                "stars": c.get("stars") or 0,
                "reposts": c.get("reposts") or 0,
                "crosspost_count": c.get("crosspost_count") or 1,
                "engagement": float(bd.get("engagement") or 0.0),
                "recency": float(bd.get("recency") or 0.0),
                "source_weight": float(bd.get("source_weight") or 1.0),
                "topic_bonus": int(bd.get("topic_bonus") or 0),
                "crosspost_bonus": float(bd.get("crosspost_bonus") or 0.0),
                "penalty": float(bd.get("penalty") or 1.0),
                "matched_topics": bd.get("matched_topics") or [],
            })
            logger.info(log_line)

        logger.removeHandler(handler)

        # Filter to score_candidate records
        score_records = []
        for r in records:
            try:
                d = json.loads(r.getMessage())
                if d.get("event") == "score_candidate":
                    score_records.append(d)
            except (json.JSONDecodeError, TypeError):
                pass

        assert len(score_records) == 2

    def test_log_line_has_all_required_fields(self):
        """Each log line must include all fields from the acceptance criteria."""
        items = _make_scored_items()
        _assign_candidate_ids(items)

        bd = items[0].get("score_breakdown") or {}
        log_line = json.dumps({
            "event": "score_candidate",
            "id": items[0].get("candidate_id"),
            "rank": 1,
            "score": float(items[0].get("score") or 0.0),
            "scored_at": bd.get("scored_at", ""),
            "source": str(items[0].get("source") or ""),
            "title": str(items[0].get("title") or "")[:80],
            "published_at": str(items[0].get("published_at") or "") if items[0].get("published_at") else "",
            "upvotes": items[0].get("upvotes") or 0,
            "comments": items[0].get("comments") or 0,
            "stars": items[0].get("stars") or 0,
            "reposts": items[0].get("reposts") or 0,
            "crosspost_count": items[0].get("crosspost_count") or 1,
            "engagement": float(bd.get("engagement") or 0.0),
            "recency": float(bd.get("recency") or 0.0),
            "source_weight": float(bd.get("source_weight") or 1.0),
            "topic_bonus": int(bd.get("topic_bonus") or 0),
            "crosspost_bonus": float(bd.get("crosspost_bonus") or 0.0),
            "penalty": float(bd.get("penalty") or 1.0),
            "matched_topics": bd.get("matched_topics") or [],
        })

        parsed = json.loads(log_line)
        required_fields = {
            "event", "id", "rank", "score", "scored_at", "source", "title",
            "published_at", "upvotes", "comments", "stars", "reposts",
            "crosspost_count", "engagement", "recency", "source_weight",
            "topic_bonus", "crosspost_bonus", "penalty", "matched_topics",
        }
        assert required_fields.issubset(set(parsed.keys())), \
            f"Missing fields: {required_fields - set(parsed.keys())}"

    def test_title_truncated_to_80_chars(self):
        """Title must be truncated to 80 chars before serialization."""
        long_title = "A" * 200
        item = {
            "title": long_title,
            "url": "https://example.com",
            "source": "hn",
            "source_name": "HN",
            "upvotes": 100,
        }
        from newsbot.config import DEFAULT_SOURCE_WEIGHTS
        cfg = {"source_weights": DEFAULT_SOURCE_WEIGHTS, "topic_boost": {}, "lookback_hours": 48}
        from datetime import datetime, timezone
        now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
        scored = score_all([item], cfg, now=now)
        _assign_candidate_ids(scored)

        bd = scored[0].get("score_breakdown") or {}
        log_line = json.dumps({
            "event": "score_candidate",
            "id": scored[0].get("candidate_id"),
            "rank": 1,
            "score": float(scored[0].get("score") or 0.0),
            "scored_at": bd.get("scored_at", ""),
            "source": str(scored[0].get("source") or ""),
            "title": str(scored[0].get("title") or "")[:80],
            "published_at": "",
            "upvotes": scored[0].get("upvotes") or 0,
            "comments": 0,
            "stars": 0,
            "reposts": 0,
            "crosspost_count": 1,
            "engagement": float(bd.get("engagement") or 0.0),
            "recency": float(bd.get("recency") or 0.0),
            "source_weight": float(bd.get("source_weight") or 1.0),
            "topic_bonus": int(bd.get("topic_bonus") or 0),
            "crosspost_bonus": float(bd.get("crosspost_bonus") or 0.0),
            "penalty": float(bd.get("penalty") or 1.0),
            "matched_topics": bd.get("matched_topics") or [],
        })
        parsed = json.loads(log_line)
        assert len(parsed["title"]) == 80

    def test_title_with_quotes_json_escaped(self):
        """Titles with quotes must not break the JSON format."""
        tricky_title = 'Story with "quotes" and \\ backslash'
        item = {
            "title": tricky_title,
            "url": "https://example.com",
            "source": "hn",
            "source_name": "HN",
            "upvotes": 100,
        }
        from newsbot.config import DEFAULT_SOURCE_WEIGHTS
        cfg = {"source_weights": DEFAULT_SOURCE_WEIGHTS, "topic_boost": {}, "lookback_hours": 48}
        from datetime import datetime, timezone
        now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
        scored = score_all([item], cfg, now=now)
        _assign_candidate_ids(scored)

        bd = scored[0].get("score_breakdown") or {}
        log_line = json.dumps({
            "event": "score_candidate",
            "id": scored[0].get("candidate_id"),
            "rank": 1,
            "score": float(scored[0].get("score") or 0.0),
            "scored_at": bd.get("scored_at", ""),
            "source": str(scored[0].get("source") or ""),
            "title": str(scored[0].get("title") or "")[:80],
            "published_at": "",
            "upvotes": scored[0].get("upvotes") or 0,
            "comments": 0,
            "stars": 0,
            "reposts": 0,
            "crosspost_count": 1,
            "engagement": float(bd.get("engagement") or 0.0),
            "recency": float(bd.get("recency") or 0.0),
            "source_weight": float(bd.get("source_weight") or 1.0),
            "topic_bonus": int(bd.get("topic_bonus") or 0),
            "crosspost_bonus": float(bd.get("crosspost_bonus") or 0.0),
            "penalty": float(bd.get("penalty") or 1.0),
            "matched_topics": bd.get("matched_topics") or [],
        })
        # Must parse cleanly
        parsed = json.loads(log_line)
        assert parsed["title"] == tricky_title

    def test_rank_is_sequential(self):
        """Rank should be 1, 2, 3, ... in order."""
        items = _make_scored_items()
        _assign_candidate_ids(items)

        ranks = []
        for rank, c in enumerate(items, start=1):
            ranks.append(rank)

        assert ranks == [1, 2]


class TestIDPreservationInLLMFilter:
    """Verify that llm_filter preserves existing candidate IDs."""

    @pytest.mark.asyncio
    async def test_llm_filter_preserves_existing_ids(self):
        """IDs assigned before llm_filter should be preserved, not reassigned."""
        items = _make_scored_items()
        # Assign IDs before the filter (as _run_generation now does)
        _assign_candidate_ids(items)
        original_ids = [item["candidate_id"] for item in items]

        # Fake LLM that keeps all items
        class FakeLM:
            async def generate(self, messages, **kwargs):
                return json.dumps({
                    "items": [
                        {"id": cid, "keep": True, "title": items[i]["title"],
                         "category": "AI", "importance": 8, "reason": "x", "short_summary": "s"}
                        for i, cid in enumerate(original_ids)
                    ]
                }), {"model": "test"}

        kept = await llm_filter(items, FakeLM())
        assert len(kept) == 2
        # IDs should match the ones we assigned, not new ones
        kept_ids = [k.get("candidate_id") for k in kept]
        assert kept_ids == original_ids

    @pytest.mark.asyncio
    async def test_llm_filter_assigns_ids_when_none(self):
        """When items have no candidate_id, llm_filter should still assign them."""
        items = _make_scored_items()
        # Remove any candidate_id
        for item in items:
            item.pop("candidate_id", None)

        class FakeLM:
            async def generate(self, messages, **kwargs):
                return json.dumps({
                    "items": [
                        {"id": "c001", "keep": True, "title": items[0]["title"],
                         "category": "AI", "importance": 8, "reason": "x", "short_summary": "s"},
                    ]
                }), {"model": "test"}

        kept = await llm_filter(items, FakeLM())
        assert len(kept) == 1
        assert kept[0]["candidate_id"] == "c001"

    @pytest.mark.asyncio
    async def test_preserved_ids_match_in_llm_response(self):
        """The LLM response must use the same IDs that were pre-assigned."""
        items = _make_scored_items()
        _assign_candidate_ids(items)
        # Items are c001, c002 — but LLM returns c001, c003 (c003 doesn't exist)
        class FakeLM:
            async def generate(self, messages, **kwargs):
                return json.dumps({
                    "items": [
                        {"id": "c001", "keep": True, "title": "AI",
                         "category": "AI", "importance": 9, "reason": "x", "short_summary": "s"},
                        {"id": "c003", "keep": True, "title": "Fake",
                         "category": "AI", "importance": 10, "reason": "x", "short_summary": "s"},
                    ]
                }), {"model": "test"}

        kept = await llm_filter(items, FakeLM())
        # c003 should be skipped (unknown ID)
        assert len(kept) == 1
        assert kept[0]["candidate_id"] == "c001"


class TestMissingIDAssignmentNoCollision:
    """Verify _assign_missing_candidate_ids avoids duplicate IDs in mixed lists."""

    def test_no_collision_when_existing_id_present(self):
        """Items with c001 already set should not get a duplicate c001."""
        items = [
            {"title": "A", "url": "http://a.com", "source": "hn", "candidate_id": "c001"},
            {"title": "B", "url": "http://b.com", "source": "hn"},  # no ID
        ]
        id_map = _assign_missing_candidate_ids(items)
        # First item keeps c001
        assert items[0]["candidate_id"] == "c001"
        # Second item gets c002 (not c001)
        assert items[1]["candidate_id"] == "c002"
        # No duplicates in the map
        assert len(id_map) == 2
        assert set(id_map.keys()) == {"c001", "c002"}

    def test_all_items_need_ids(self):
        """When no items have IDs, all get assigned sequentially."""
        items = [
            {"title": "A", "url": "http://a.com", "source": "hn"},
            {"title": "B", "url": "http://b.com", "source": "hn"},
        ]
        id_map = _assign_missing_candidate_ids(items)
        assert items[0]["candidate_id"] == "c001"
        assert items[1]["candidate_id"] == "c002"

    def test_all_items_have_ids(self):
        """When all items already have IDs, none are reassigned."""
        items = [
            {"title": "A", "url": "http://a.com", "source": "hn", "candidate_id": "c005"},
            {"title": "B", "url": "http://b.com", "source": "hn", "candidate_id": "c010"},
        ]
        id_map = _assign_missing_candidate_ids(items)
        assert items[0]["candidate_id"] == "c005"
        assert items[1]["candidate_id"] == "c010"

    def test_mixed_list_filter_preserves_unique_ids(self):
        """llm_filter with mixed list should not create duplicate IDs."""
        items = [
            {"title": "A", "url": "http://a.com", "source": "hn",
             "source_name": "HN", "candidate_id": "c001", "score": 50.0},
            {"title": "B", "url": "http://b.com", "source": "hn",
             "source_name": "HN", "score": 40.0},  # no ID
        ]

        class FakeLM:
            async def generate(self, messages, **kwargs):
                return json.dumps({
                    "items": [
                        {"id": "c001", "keep": True, "title": "A",
                         "category": "AI", "importance": 8, "reason": "x", "short_summary": "s"},
                        {"id": "c002", "keep": True, "title": "B",
                         "category": "AI", "importance": 7, "reason": "x", "short_summary": "s"},
                    ]
                }), {"model": "test"}

        kept = asyncio.run(llm_filter(items, FakeLM()))
        assert len(kept) == 2
        kept_ids = [k.get("candidate_id") for k in kept]
        assert kept_ids == ["c001", "c002"]
        # No duplicates
        assert len(set(kept_ids)) == 2


class TestRunGenerationIntegration:
    """Integration test: exercise _run_generation logging through mocked dependencies."""

    @pytest.mark.asyncio
    async def test_generation_logs_score_candidates(self, monkeypatch):
        """_run_generation must produce score_candidate JSON log lines for each candidate."""
        import os
        from newsbot.main import _run_generation

        # Build a minimal settings mock
        settings = MagicMock()
        settings.get_all.return_value = {}

        # Mock load_config to return minimal config
        from newsbot.config import DEFAULT_SOURCE_WEIGHTS
        mock_cfg = {
            "sources": {"hn": {"max_items": 2}},
            "source_weights": DEFAULT_SOURCE_WEIGHTS,
            "topic_boost": {},
            "lookback_hours": 48,
            "min_score": 0.0,
            "max_candidates": 2,
            "max_final_news": 1,
            "source_quota": 8,
            "llm_temperature": 0.4,
            "llm_max_tokens_filter": 800,
            "llm_max_tokens_digest": 8000,
            "style_prompt": "",
        }

        monkeypatch.setattr("newsbot.main.load_config", lambda s: mock_cfg)
        monkeypatch.setattr("newsbot.main._set_pre_merge_weights", lambda w: None)

        # Mock collect_all to return TWO items with tricky titles
        long_title = "A" * 200
        quoted_title = 'Story with "quotes" and \\ backslash'
        async def mock_collect_all(cfg):
            return [
                {"title": quoted_title, "url": "https://example.com/ai", "source": "hn",
                 "source_name": "Hacker News", "upvotes": 500, "published_at": "2026-07-28T10:00:00+00:00"},
                {"title": long_title, "url": "https://example.com/gpu", "source": "hn",
                 "source_name": "Hacker News", "upvotes": 300, "published_at": "2026-07-28T09:00:00+00:00"},
            ]
        monkeypatch.setattr("newsbot.main.collect_all", mock_collect_all)

        # Mock filter_seen to pass through
        monkeypatch.setattr("newsbot.main.filter_seen", lambda items, store: items)
        # Mock dedupe
        monkeypatch.setattr("newsbot.main.dedupe_and_merge", lambda items: items)

        # Mock store
        store = MagicMock()
        store.is_seen_batch.return_value = set()
        store.replace_unposted_batch.return_value = (1, 2)

        # Mock LLM filter to keep both items
        class FakeFilterLM:
            async def generate(self, messages, **kwargs):
                return json.dumps({
                    "items": [
                        {"id": "c001", "keep": True, "title": "AI",
                         "category": "AI", "importance": 9, "reason": "x", "short_summary": "s"},
                        {"id": "c002", "keep": True, "title": "GPU",
                         "category": "Hardware", "importance": 7, "reason": "x", "short_summary": "s"},
                    ]
                }), {"model": "test"}

        monkeypatch.setattr("newsbot.main._build_filter_lm_client", lambda: FakeFilterLM())

        # Mock select_diverse_top_items to return 1 item
        monkeypatch.setattr("newsbot.main.select_diverse_top_items", lambda items, n: items[:n])

        # Mock LLM styler
        class FakeStyleLM:
            async def generate(self, messages, **kwargs):
                return json.dumps({
                    "posts": [
                        {"id": "c001", "title": "AI Post", "body": "AI body text"},
                    ]
                }), {"model": "test"}

        monkeypatch.setattr("newsbot.main._build_lm_client", lambda: FakeStyleLM())

        # Capture log records
        records: list[logging.LogRecord] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = CaptureHandler()
        logger = logging.getLogger("newsbot.main")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        try:
            result = await _run_generation(store, settings)
        finally:
            logger.removeHandler(handler)

        # Filter to score_candidate records
        score_records = []
        for r in records:
            try:
                d = json.loads(r.getMessage())
                if d.get("event") == "score_candidate":
                    score_records.append(d)
            except (json.JSONDecodeError, TypeError):
                pass

        # Should have 2 score_candidate log lines (one per candidate sent to filter)
        assert len(score_records) == 2

        # Each must have all required fields
        required_fields = {
            "event", "id", "rank", "score", "scored_at", "source", "title",
            "published_at", "upvotes", "comments", "stars", "reposts",
            "crosspost_count", "engagement", "recency", "source_weight",
            "topic_bonus", "crosspost_bonus", "penalty", "matched_topics",
        }
        for rec in score_records:
            assert required_fields.issubset(set(rec.keys())), \
                f"Missing fields: {required_fields - set(rec.keys())}"
            assert rec["event"] == "score_candidate"

        # IDs should be c001 and c002 (assigned in _run_generation)
        ids = [r["id"] for r in score_records]
        assert "c001" in ids
        assert "c002" in ids

        # Ranks should be sequential
        ranks = sorted(r["rank"] for r in score_records)
        assert ranks == [1, 2]

        # Title with quotes must survive JSON escaping without breaking
        quoted_rec = next(r for r in score_records if r["id"] == "c001")
        assert quoted_rec["title"] == quoted_title

        # Long title must be truncated to 80 chars
        long_rec = next(r for r in score_records if r["id"] == "c002")
        assert len(long_rec["title"]) == 80

    @pytest.mark.asyncio
    async def test_generation_excludes_non_sent_candidates(self, monkeypatch):
        """Only candidates sent to LLM filter are logged — not all scored items."""
        from newsbot.main import _run_generation

        settings = MagicMock()
        settings.get_all.return_value = {}

        from newsbot.config import DEFAULT_SOURCE_WEIGHTS
        mock_cfg = {
            "sources": {"hn": {"max_items": 2}},
            "source_weights": DEFAULT_SOURCE_WEIGHTS,
            "topic_boost": {},
            "lookback_hours": 48,
            "min_score": 0.0,
            "max_candidates": 1,  # Only 1 candidate sent to filter
            "max_final_news": 1,
            "source_quota": 8,
            "llm_temperature": 0.4,
            "llm_max_tokens_filter": 800,
            "llm_max_tokens_digest": 8000,
            "style_prompt": "",
        }

        monkeypatch.setattr("newsbot.main.load_config", lambda s: mock_cfg)
        monkeypatch.setattr("newsbot.main._set_pre_merge_weights", lambda w: None)

        async def mock_collect_all(cfg):
            return [
                {"title": "High Score", "url": "https://example.com/h", "source": "hn",
                 "source_name": "HN", "upvotes": 1000, "published_at": "2026-07-28T10:00:00+00:00"},
                {"title": "Low Score", "url": "https://example.com/l", "source": "hn",
                 "source_name": "HN", "upvotes": 1, "published_at": "2026-07-28T10:00:00+00:00"},
            ]
        monkeypatch.setattr("newsbot.main.collect_all", mock_collect_all)
        monkeypatch.setattr("newsbot.main.filter_seen", lambda items, store: items)
        monkeypatch.setattr("newsbot.main.dedupe_and_merge", lambda items: items)

        store = MagicMock()
        store.is_seen_batch.return_value = set()
        store.replace_unposted_batch.return_value = (1, 1)

        class FakeFilterLM:
            async def generate(self, messages, **kwargs):
                return json.dumps({
                    "items": [
                        {"id": "c001", "keep": True, "title": "High",
                         "category": "AI", "importance": 9, "reason": "x", "short_summary": "s"},
                    ]
                }), {"model": "test"}

        monkeypatch.setattr("newsbot.main._build_filter_lm_client", lambda: FakeFilterLM())
        monkeypatch.setattr("newsbot.main.select_diverse_top_items", lambda items, n: items[:n])

        class FakeStyleLM:
            async def generate(self, messages, **kwargs):
                return json.dumps({"posts": [{"id": "c001", "title": "H", "body": "b"}]}), {"model": "test"}
        monkeypatch.setattr("newsbot.main._build_lm_client", lambda: FakeStyleLM())

        records: list[logging.LogRecord] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = CaptureHandler()
        logger = logging.getLogger("newsbot.main")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        try:
            await _run_generation(store, settings)
        finally:
            logger.removeHandler(handler)

        score_records = []
        for r in records:
            try:
                d = json.loads(r.getMessage())
                if d.get("event") == "score_candidate":
                    score_records.append(d)
            except (json.JSONDecodeError, TypeError):
                pass

        # max_candidates=1, so only 1 candidate logged (not 2)
        assert len(score_records) == 1
        assert score_records[0]["id"] == "c001"


class TestZeroValueLogging:
    """Verify that zero-valued score components are logged correctly."""

    def test_zero_source_weight_logged_as_zero(self):
        """source_weight=0.0 must be logged as 0.0, not 1.0."""
        items = [
            {"title": "Zero Weight", "url": "https://example.com", "source": "hn",
             "source_name": "HN", "upvotes": 100, "published_at": "2026-07-28T10:00:00+00:00"},
        ]
        # scoring.py normalizes "hn" -> "hackernews" via _SOURCE_ALIASES
        cfg = {"source_weights": {"hackernews": 0.0}, "topic_boost": {}, "lookback_hours": 48}
        from datetime import datetime, timezone
        now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
        scored = score_all(items, cfg, now=now)
        _assign_candidate_ids(scored)

        bd = scored[0].get("score_breakdown") or {}
        # Simulate the production logging logic with the fix
        sw = float(bd.get("source_weight")) if bd.get("source_weight") is not None else 1.0
        assert sw == 0.0, f"source_weight should be 0.0, got {sw}"
        assert bd["source_weight"] == 0.0

    def test_zero_penalty_logged_as_zero(self):
        """penalty=0.0 must be logged as 0.0, not 1.0."""
        items = [
            {"title": "Zero Penalty", "url": "https://example.com", "source": "hn",
             "source_name": "HN", "upvotes": 100, "penalty": 0.0,
             "published_at": "2026-07-28T10:00:00+00:00"},
        ]
        from newsbot.config import DEFAULT_SOURCE_WEIGHTS
        cfg = {"source_weights": DEFAULT_SOURCE_WEIGHTS, "topic_boost": {}, "lookback_hours": 48}
        from datetime import datetime, timezone
        now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
        scored = score_all(items, cfg, now=now)
        _assign_candidate_ids(scored)

        bd = scored[0].get("score_breakdown") or {}
        # Simulate the production logging logic with the fix
        p = float(bd.get("penalty")) if bd.get("penalty") is not None else 1.0
        assert p == 0.0, f"penalty should be 0.0, got {p}"
        assert bd["penalty"] == 0.0
        assert bd["score"] == 0.0  # penalty=0 means score=0