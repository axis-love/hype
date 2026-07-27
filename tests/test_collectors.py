"""Tests for the HN and Reddit collectors — engagement signal capture.

HN uses httpx (Algolia API). Reddit uses feedparser (RSS feeds).
Tests mock the current network and parsing boundaries.
"""

import json
from unittest.mock import AsyncMock, patch, MagicMock
from typing import Any

import pytest

from newsbot.collectors import hackernews as hn
from newsbot.collectors import reddit


# --- Hacker News --------------------------------------------------------

def _hn_hit(title="Tool X", url="https://x.com", points=430, comments=180, object_id="42"):
    return {
        "title": title,
        "url": url,
        "points": points,
        "num_comments": comments,
        "objectID": object_id,
        "story_text": "story body",
        "created_at": "2026-07-04T10:00:00.000000+00:00",
    }


@pytest.mark.asyncio
async def test_hn_collect_captures_points_and_comments():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"hits": [_hn_hit()]}
    fake_resp.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.collectors.hackernews.httpx.AsyncClient", return_value=fake_client):
        items = await hn.collect({"tags": "front_page", "limit": 10})

    assert len(items) == 1
    assert items[0]["source"] == "hn"
    assert items[0]["upvotes"] == 430
    assert items[0]["comments"] == 180
    assert items[0]["published_at"].startswith("2026-07-04")


@pytest.mark.asyncio
async def test_hn_collect_falls_back_to_hn_url_when_url_missing():
    hit = _hn_hit(url="")
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"hits": [hit]}
    fake_resp.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.collectors.hackernews.httpx.AsyncClient", return_value=fake_client):
        items = await hn.collect({"tags": "front_page", "limit": 10})

    assert items[0]["url"] == "https://news.ycombinator.com/item?id=42"


# --- Reddit (RSS-based) -------------------------------------------------

def _reddit_rss_entry(title="Tool X — 2.1k votes, 340 comments",
                      link="https://www.reddit.com/r/LocalLLaMA/comments/abc/tool_x/",
                      published="2026-07-04T10:00:00+00:00",
                      summary="<p>Some body text</p>"):
    """Create a fake feedparser entry as returned by Reddit RSS."""
    return {
        "title": title,
        "link": link,
        "published": published,
        "summary": summary,
    }


def _make_parsed_feed(entries: list[dict[str, Any]]) -> MagicMock:
    """Create a fake feedparser.ParseResult."""
    feed = MagicMock()
    feed.status = 200
    feed.entries = entries
    return feed


@pytest.mark.asyncio
async def test_reddit_collect_captures_score_and_comments_from_rss():
    """Reddit collector uses RSS; engagement is parsed from entry title."""
    entry = _reddit_rss_entry()
    parsed = _make_parsed_feed([entry])

    mock_response = MagicMock()
    mock_response.content = b"<rss>mock</rss>"
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=mock_client):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=parsed)
            items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert len(items) == 1
    assert items[0]["source"] == "reddit"
    assert items[0]["source_name"] == "r/LocalLLaMA"
    assert items[0]["upvotes"] == 2100  # parsed from "2.1k votes"
    assert items[0]["comments"] == 340
    assert items[0]["url"] == "https://www.reddit.com/r/LocalLLaMA/comments/abc/tool_x/"


@pytest.mark.asyncio
async def test_reddit_collect_handles_empty_feed():
    """Empty RSS feed should return empty list."""
    parsed = _make_parsed_feed([])

    mock_response = MagicMock()
    mock_response.content = b"<rss>mock</rss>"
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=mock_client):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=parsed)
            items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert items == []


@pytest.mark.asyncio
async def test_reddit_collect_handles_missing_engagement():
    """Reddit RSS entry without vote/comment counts in title."""
    entry = _reddit_rss_entry(title="Just a title without counts")
    parsed = _make_parsed_feed([entry])

    # Mock httpx to return the feed XML content, and feedparser to parse it.
    mock_response = MagicMock()
    mock_response.content = b"<rss>mock</rss>"
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=mock_client):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=parsed)
            items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert len(items) == 1
    assert items[0]["upvotes"] is None  # no count in title
    assert items[0]["comments"] is None


@pytest.mark.asyncio
async def test_reddit_collect_multiple_subreddits():
    """Multiple subreddits should all be fetched."""
    entry1 = _reddit_rss_entry(title="Story 1 — 100 votes, 10 comments")
    entry2 = _reddit_rss_entry(title="Story 2 — 200 votes, 20 comments",
                               link="https://www.reddit.com/r/MachineLearning/comments/xyz/story2/")
    parsed1 = _make_parsed_feed([entry1])
    parsed2 = _make_parsed_feed([entry2])

    call_count = 0
    def mock_parse(content):
        nonlocal call_count
        call_count += 1
        return parsed1 if call_count == 1 else parsed2

    mock_response = MagicMock()
    mock_response.content = b"<rss>mock</rss>"
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=mock_client):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = mock_parse
            items = await reddit.collect({"subreddits": ["LocalLLaMA", "MachineLearning"], "limit": 10})

    assert len(items) == 2
    assert items[0]["source_name"] == "r/LocalLLaMA"
    assert items[1]["source_name"] == "r/MachineLearning"