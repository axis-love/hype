"""Tests for newsbot/summarizer.py — Pass A JSON parse, non-empty guard, diversity, ID binding."""

import json
from typing import Any

import pytest

from newsbot.summarizer import llm_filter, llm_style_posts, select_diverse_top_items


class _FakeLM:
    """Fake LMClient that returns a canned response."""

    def __init__(self, response_text: str, finish: str = "stop"):
        self.model = "fake-model"
        self._response = response_text
        self._finish = finish
        self.last_request = None

    async def generate(self, messages, **params):
        self.last_request = {"messages": messages, **params}
        return self._response, self._finish


def _candidate(title, url, **extra):
    return {
        "title": title,
        "url": url,
        "source": "hn",
        "source_name": "Hacker News",
        "snippet": "snippet",
        "upvotes": 100,
        "comments": 20,
        "stars": None,
        "forks": None,
        "crosspost_count": 1,
        "score": 50.0,
        **extra,
    }


@pytest.mark.asyncio
async def test_llm_filter_parses_valid_json_and_keeps_marked_items():
    """Filter keeps items with keep=True, matches by candidate ID."""
    payload = {
        "items": [
            {"id": "c001", "keep": True, "title": "Good news",
             "category": "AI", "importance": 8, "reason": "r", "short_summary": "s"},
            {"id": "c002", "keep": False, "title": "Spam",
             "category": "AI", "importance": 1, "reason": "r", "short_summary": "s"},
        ]
    }
    lm = _FakeLM(json.dumps(payload))
    items = [_candidate("Good news", "https://a.com"), _candidate("Spam", "https://b.com")]
    kept = await llm_filter(items, lm)
    assert len(kept) == 1
    assert kept[0]["title"] == "Good news"
    assert kept[0]["importance"] == 8
    assert kept[0]["category"] == "AI"


@pytest.mark.asyncio
async def test_llm_filter_empty_output_returns_empty():
    lm = _FakeLM("")
    kept = await llm_filter([_candidate("X", "https://x.com")], lm)
    assert kept == []


@pytest.mark.asyncio
async def test_llm_filter_invalid_json_returns_empty():
    lm = _FakeLM("not json at all")
    kept = await llm_filter([_candidate("X", "https://x.com")], lm)
    assert kept == []


@pytest.mark.asyncio
async def test_llm_filter_strips_think_blocks():
    raw = "<think>reasoning here</think>\n" + json.dumps({
        "items": [{"id": "c001", "keep": True, "title": "T",
                   "category": "AI", "importance": 7, "reason": "r", "short_summary": "s"}]
    })
    lm = _FakeLM(raw)
    kept = await llm_filter([_candidate("T", "https://t.com")], lm)
    assert len(kept) == 1
    assert kept[0]["title"] == "T"


@pytest.mark.asyncio
async def test_llm_filter_preserves_original_url():
    """URL must come from trusted app data, not LLM output."""
    payload = {
        "items": [
            {"id": "c001", "keep": True, "title": "T", "url": "https://evil.com/hacked",
             "category": "AI", "importance": 7, "reason": "r", "short_summary": "s"},
        ]
    }
    lm = _FakeLM(json.dumps(payload))
    items = [_candidate("T", "https://trusted.com/real")]
    kept = await llm_filter(items, lm)
    assert len(kept) == 1
    assert kept[0]["url"] == "https://trusted.com/real"


@pytest.mark.asyncio
async def test_llm_style_posts_basic():
    """Style posts match by candidate ID, URLs from trusted data."""
    items = [
        _candidate("Story A", "https://a.com", candidate_id="c001"),
        _candidate("Story B", "https://b.com", candidate_id="c002"),
    ]
    payload = {
        "posts": [
            {"id": "c001", "title": "A Post", "body": "Body A"},
            {"id": "c002", "title": "B Post", "body": "Body B"},
        ]
    }
    lm = _FakeLM(json.dumps(payload))
    posts = await llm_style_posts(items, lm)
    assert len(posts) == 2
    assert posts[0]["url"] == "https://a.com"
    assert posts[1]["url"] == "https://b.com"


def test_select_diverse_top_items_caps_per_category():
    items = [
        {"title": "a", "category": "AI", "importance": 10},
        {"title": "b", "category": "AI", "importance": 9},
        {"title": "c", "category": "AI", "importance": 8},
        {"title": "d", "category": "Game Dev", "importance": 7},
        {"title": "e", "category": "Robotics", "importance": 6},
    ]
    selected = select_diverse_top_items(items, max_items=4)
    # AI cap = 4//2 + 1 = 3, so at most 3 AI items, then fill remaining.
    ai_count = sum(1 for s in selected if s["category"] == "AI")
    assert ai_count <= 3
    assert len(selected) == 4


def test_select_diverse_top_items_handles_empty():
    assert select_diverse_top_items([], max_items=5) == []