"""Tests for newsbot/summarizer.py — Pass A JSON parse, non-empty guard, diversity."""

import json
from typing import Any

import pytest

from newsbot.summarizer import llm_filter, llm_write_digest, select_diverse_top_items


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
    payload = {
        "items": [
            {"keep": True, "title": "Good news", "url": "https://a.com",
             "category": "AI", "importance": 8, "reason": "r", "short_summary": "s"},
            {"keep": False, "title": "Spam", "url": "https://b.com",
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
        "items": [{"keep": True, "title": "T", "url": "https://t.com",
                   "category": "AI", "importance": 7, "reason": "r", "short_summary": "s"}]
    })
    lm = _FakeLM(raw)
    kept = await llm_filter([_candidate("T", "https://t.com")], lm)
    assert len(kept) == 1
    assert kept[0]["title"] == "T"


@pytest.mark.asyncio
async def test_llm_write_digest_returns_nonempty_markdown():
    items = [
        {"title": "Item one", "url": "https://one.com", "short_summary": "Summary one",
         "reason": "Reason one", "stars": 2400, "upvotes": 530, "comments": 190,
         "crosspost_count": 1},
        {"title": "Item two", "url": "https://two.com", "short_summary": "Summary two",
         "reason": "Reason two", "stars": None, "upvotes": 200, "comments": 50,
         "crosspost_count": 2},
    ]
    lm = _FakeLM("🔥 Tech / AI Digest\n\n1. Item one\nWhat happened: ...\n")
    out = await llm_write_digest(items, lm)
    assert out  # non-empty


@pytest.mark.asyncio
async def test_llm_write_digest_empty_output_returns_empty():
    lm = _FakeLM("")
    out = await llm_write_digest([{"title": "x", "url": "u"}], lm)
    assert out == ""


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