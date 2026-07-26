"""Tests for LLM output bound to immutable article identities (flow_001023).

Verifies that URLs are never accepted from LLM output, results are joined
by candidate ID not array position, and reordered/omitted/duplicated/
hallucinated model entries are handled safely.
"""
import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from newsbot.summarizer import (
    _assign_candidate_ids,
    llm_filter,
    llm_style_posts,
    select_diverse_top_items,
)


class FakeLMClient:
    """Fake LM client that returns a pre-configured JSON response."""

    def __init__(self, response_json: str):
        self._response = response_json

    async def generate(self, messages, **kwargs):
        return self._response, {"model": "test"}


@pytest.fixture
def sample_items() -> list[dict[str, Any]]:
    return [
        {"title": "AI Breakthrough", "url": "https://example.com/ai", "source": "hn",
         "source_name": "Hacker News", "score": 50.0, "upvotes": 500, "published_at": "2026-07-25"},
        {"title": "New GPU Released", "url": "https://example.com/gpu", "source": "github",
         "source_name": "GitHub", "score": 40.0, "stars": 5000, "published_at": "2026-07-25"},
        {"title": "Robot Demo", "url": "https://example.com/robot", "source": "reddit",
         "source_name": "Reddit", "score": 30.0, "upvotes": 200, "published_at": "2026-07-25"},
    ]


class TestAssignCandidateIds:
    def test_ids_assigned_sequentially(self, sample_items):
        id_map = _assign_candidate_ids(sample_items)
        assert len(id_map) == 3
        assert "c001" in id_map
        assert "c002" in id_map
        assert "c003" in id_map
        assert id_map["c001"]["title"] == "AI Breakthrough"
        assert sample_items[0]["candidate_id"] == "c001"

    def test_ids_are_3_digit_padded(self):
        items = [{"title": f"T{i}"} for i in range(5)]
        id_map = _assign_candidate_ids(items)
        assert "c001" in id_map
        assert "c005" in id_map


class TestLLMFilterIDBinding:
    @pytest.mark.asyncio
    async def test_filter_matches_by_id_not_title(self, sample_items):
        """LLM renames the title but echoes the ID — should still match."""
        response = json.dumps({
            "items": [
                {"id": "c001", "keep": True, "title": "Renamed AI Story",
                 "category": "AI", "importance": 9, "reason": "big", "short_summary": "s"},
                {"id": "c002", "keep": True, "title": "GPU News",
                 "category": "Hardware", "importance": 7, "reason": "good", "short_summary": "s"},
                {"id": "c003", "keep": False, "title": "Robot Demo"},
            ]
        })
        lm = FakeLMClient(response)
        kept = await llm_filter(sample_items, lm)
        assert len(kept) == 2
        # URL should be from original data, not LLM
        assert kept[0]["url"] == "https://example.com/ai"
        assert kept[0]["title"] == "Renamed AI Story"  # LLM's title accepted
        assert kept[1]["url"] == "https://example.com/gpu"

    @pytest.mark.asyncio
    async def test_filter_ignores_hallucinated_url(self, sample_items):
        """LLM returns a url field — it should be ignored."""
        response = json.dumps({
            "items": [
                {"id": "c001", "keep": True, "title": "AI",
                 "url": "https://evil.com/hacked", "category": "AI",
                 "importance": 9, "reason": "x", "short_summary": "s"},
            ]
        })
        lm = FakeLMClient(response)
        kept = await llm_filter(sample_items, lm)
        assert len(kept) == 1
        assert kept[0]["url"] == "https://example.com/ai"  # original, not evil.com

    @pytest.mark.asyncio
    async def test_filter_skips_unknown_id(self, sample_items):
        """LLM returns an ID that doesn't exist — should be skipped."""
        response = json.dumps({
            "items": [
                {"id": "c001", "keep": True, "title": "AI", "category": "AI",
                 "importance": 9, "reason": "x", "short_summary": "s"},
                {"id": "c999", "keep": True, "title": "Fake", "category": "X",
                 "importance": 10, "reason": "x", "short_summary": "s"},
            ]
        })
        lm = FakeLMClient(response)
        kept = await llm_filter(sample_items, lm)
        assert len(kept) == 1
        assert kept[0]["title"] == "AI"

    @pytest.mark.asyncio
    async def test_filter_skips_duplicate_id(self, sample_items):
        """LLM returns the same ID twice — second should be skipped."""
        response = json.dumps({
            "items": [
                {"id": "c001", "keep": True, "title": "First", "category": "AI",
                 "importance": 9, "reason": "x", "short_summary": "s"},
                {"id": "c001", "keep": True, "title": "Duplicate", "category": "AI",
                 "importance": 10, "reason": "x", "short_summary": "s"},
            ]
        })
        lm = FakeLMClient(response)
        kept = await llm_filter(sample_items, lm)
        assert len(kept) == 1
        assert kept[0]["title"] == "First"

    @pytest.mark.asyncio
    async def test_filter_skips_missing_id(self, sample_items):
        """LLM returns no id field — should be skipped."""
        response = json.dumps({
            "items": [
                {"keep": True, "title": "No ID", "category": "AI",
                 "importance": 9, "reason": "x", "short_summary": "s"},
                {"id": "c002", "keep": True, "title": "Has ID", "category": "HW",
                 "importance": 7, "reason": "x", "short_summary": "s"},
            ]
        })
        lm = FakeLMClient(response)
        kept = await llm_filter(sample_items, lm)
        assert len(kept) == 1
        assert kept[0]["title"] == "Has ID"


class TestLLMStyleIDBinding:
    @pytest.mark.asyncio
    async def test_style_matches_by_id_not_index(self, sample_items):
        """LLM reorders posts — URLs should still match by ID."""
        # Assign IDs first (filter pass would have done this)
        _assign_candidate_ids(sample_items)
        # LLM returns posts in reverse order
        response = json.dumps({
            "posts": [
                {"id": "c003", "title": "Robot Post", "body": "Robot body"},
                {"id": "c002", "title": "GPU Post", "body": "GPU body"},
                {"id": "c001", "title": "AI Post", "body": "AI body"},
            ]
        })
        lm = FakeLMClient(response)
        posts = await llm_style_posts(sample_items, lm)
        assert len(posts) == 3
        # URL should match by ID, not by array position
        assert posts[0]["url"] == "https://example.com/robot"
        assert posts[1]["url"] == "https://example.com/gpu"
        assert posts[2]["url"] == "https://example.com/ai"

    @pytest.mark.asyncio
    async def test_style_omitted_post_does_not_shift_urls(self, sample_items):
        """If LLM omits the second post, the third should not get the second's URL."""
        _assign_candidate_ids(sample_items)
        # LLM omits c002
        response = json.dumps({
            "posts": [
                {"id": "c001", "title": "AI Post", "body": "AI body"},
                {"id": "c003", "title": "Robot Post", "body": "Robot body"},
            ]
        })
        lm = FakeLMClient(response)
        posts = await llm_style_posts(sample_items, lm)
        assert len(posts) == 2
        assert posts[0]["url"] == "https://example.com/ai"
        assert posts[1]["url"] == "https://example.com/robot"  # NOT the GPU url

    @pytest.mark.asyncio
    async def test_style_ignores_hallucinated_url(self, sample_items):
        """LLM returns a url field in style output — should be ignored."""
        _assign_candidate_ids(sample_items)
        response = json.dumps({
            "posts": [
                {"id": "c001", "title": "AI", "body": "text", "url": "https://evil.com"},
            ]
        })
        lm = FakeLMClient(response)
        posts = await llm_style_posts(sample_items, lm)
        assert len(posts) == 1
        assert posts[0]["url"] == "https://example.com/ai"  # original, not evil.com

    @pytest.mark.asyncio
    async def test_style_skips_unknown_id(self, sample_items):
        """LLM returns an unknown ID — should be skipped."""
        _assign_candidate_ids(sample_items)
        response = json.dumps({
            "posts": [
                {"id": "c001", "title": "AI", "body": "text"},
                {"id": "cXXX", "title": "Fake", "body": "fake text"},
            ]
        })
        lm = FakeLMClient(response)
        posts = await llm_style_posts(sample_items, lm)
        assert len(posts) == 1
        assert posts[0]["title"] == "AI"

    @pytest.mark.asyncio
    async def test_style_skips_duplicate_id(self, sample_items):
        """LLM returns the same ID twice — second should be skipped."""
        _assign_candidate_ids(sample_items)
        response = json.dumps({
            "posts": [
                {"id": "c001", "title": "First", "body": "text1"},
                {"id": "c001", "title": "Dup", "body": "text2"},
            ]
        })
        lm = FakeLMClient(response)
        posts = await llm_style_posts(sample_items, lm)
        assert len(posts) == 1
        assert posts[0]["title"] == "First"