"""Tests for newsbot/summarizer.py — Pass A JSON parse, non-empty guard, diversity, ID binding."""

import json
from typing import Any

import pytest

from newsbot.summarizer import llm_daily_summary, llm_filter, llm_style_posts, select_diverse_top_items


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

def test_unknown_llm_id_not_logged_raw(caplog):
    """Unknown LLM IDs should not appear raw in logs — only their length."""
    import logging
    import json
    from unittest.mock import AsyncMock
    from newsbot.summarizer import llm_filter

    items = [
        {"title": "Test", "url": "https://example.com/1", "source": "hn",
         "source_name": "HN", "candidate_id": "c001", "score": 1.0}
    ]
    # Model returns an ID containing prompt-like content
    malicious_id = "PROMPT_INJECTION_ATTEMPT_WITH_ARTICLE_TEXT_" + "x" * 200
    raw = json.dumps({"items": [{"id": malicious_id, "keep": True, "title": "T"}]})

    lm_client = AsyncMock()
    lm_client.generate = AsyncMock(return_value=(raw, {}))

    caplog.set_level(logging.WARNING)
    import asyncio
    result = asyncio.run(llm_filter(items, lm_client))

    # Check that the raw malicious ID is NOT in any log message
    for record in caplog.records:
        assert malicious_id not in record.getMessage(), \
            f"Raw LLM ID leaked into log: {record.getMessage()}"
    # But the length should be mentioned
    assert any("len=" in r.getMessage() for r in caplog.records)


# --- llm_daily_summary (OQ-1 recap contract) ----------------------------

_RECAP_PROMPT = "Return strict JSON {title, items:[{id, summary}]}."


def _recap_item(title, url, **extra) -> dict[str, Any]:
    return {
        "title": title,
        "body": f"Styled body for {title}",
        "category": "AI",
        "source": "hn",
        "posted_at": "2026-08-16T06:00:00+00:00",
        "url": url,
        "message_id": extra.pop("message_id", None),
        **extra,
    }


@pytest.mark.asyncio
async def test_llm_daily_summary_binds_by_id_and_merges_trusted_fields():
    """LLM items bind to inputs by app-assigned id; title/url/message_id
    always come from trusted app data, never from LLM output."""
    payload = {
        "title": "Day recap",
        "items": [
            {"id": "c002", "summary": "Second post first", "title": "LLM-FAKED TITLE",
             "url": "https://evil.example/hijack"},
            {"id": "c001", "summary": "First post second"},
        ],
    }
    lm = _FakeLM(json.dumps(payload))
    items = [
        _recap_item("Post A", "https://trusted.example/a"),
        _recap_item("Post B", "https://trusted.example/b", message_id=42),
    ]
    result = await llm_daily_summary(items, lm, recap_prompt=_RECAP_PROMPT)
    assert result is not None
    assert result["title"] == "Day recap"
    # Order follows the LLM (importance), not input order.
    assert [i["id"] for i in result["items"]] == ["c002", "c001"]
    first = result["items"][0]
    assert first["summary"] == "Second post first"
    assert first["title"] == "Post B"  # trusted title, LLM title ignored
    assert first["url"] == "https://trusted.example/b"  # trusted url
    assert first["message_id"] == 42
    # IDs were assigned to the input items for the prompt.
    assert items[0]["candidate_id"] == "c001"
    assert items[1]["candidate_id"] == "c002"


@pytest.mark.asyncio
async def test_llm_daily_summary_skips_unknown_and_duplicate_ids(caplog):
    payload = {
        "title": "Day recap",
        "items": [
            {"id": "c001", "summary": "Valid"},
            {"id": "c999", "summary": "Unknown id"},
            {"id": "c001", "summary": "Duplicate"},
            {"id": "", "summary": "Missing id"},
        ],
    }
    lm = _FakeLM(json.dumps(payload))
    items = [_recap_item("Post A", "https://a.example")]
    result = await llm_daily_summary(items, lm, recap_prompt=_RECAP_PROMPT)
    assert result is not None
    assert [i["id"] for i in result["items"]] == ["c001"]
    assert result["items"][0]["summary"] == "Valid"
    messages = [r.getMessage() for r in caplog.records]
    assert any("unknown id" in m for m in messages)
    assert any("duplicate id" in m for m in messages)


@pytest.mark.asyncio
async def test_llm_daily_summary_warns_on_omitted_items(caplog):
    payload = {"title": "Day recap", "items": [{"id": "c001", "summary": "Only one"}]}
    lm = _FakeLM(json.dumps(payload))
    items = [
        _recap_item("Post A", "https://a.example"),
        _recap_item("Post B", "https://b.example"),
    ]
    result = await llm_daily_summary(items, lm, recap_prompt=_RECAP_PROMPT)
    assert result is not None
    assert len(result["items"]) == 1
    assert any("omitted" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_llm_daily_summary_strips_think_blocks():
    raw = "<think>reasoning</think>\n" + json.dumps({
        "title": "T", "items": [{"id": "c001", "summary": "S"}],
    })
    lm = _FakeLM(raw)
    result = await llm_daily_summary(
        [_recap_item("Post A", "https://a.example")], lm, recap_prompt=_RECAP_PROMPT,
    )
    assert result is not None
    assert result["title"] == "T"
    assert result["items"][0]["summary"] == "S"


@pytest.mark.asyncio
async def test_llm_daily_summary_empty_recap_prompt_returns_none():
    lm = _FakeLM(json.dumps({"title": "T", "items": []}))
    result = await llm_daily_summary(
        [_recap_item("Post A", "https://a.example")], lm, recap_prompt="",
    )
    assert result is None


@pytest.mark.asyncio
async def test_llm_daily_summary_invalid_json_returns_none():
    lm = _FakeLM("not json at all")
    result = await llm_daily_summary(
        [_recap_item("Post A", "https://a.example")], lm, recap_prompt=_RECAP_PROMPT,
    )
    assert result is None


@pytest.mark.asyncio
async def test_llm_daily_summary_empty_items_returns_none():
    lm = _FakeLM(json.dumps({"title": "T", "items": []}))
    assert await llm_daily_summary([], lm, recap_prompt=_RECAP_PROMPT) is None
