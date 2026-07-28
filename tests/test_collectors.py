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


# --- Candidate boundary type tests --------------------------------------

from newsbot.collectors.base import Candidate
from newsbot.collectors.base import new_candidate


@pytest.mark.asyncio
async def test_hn_collect_returns_candidate_instances():
    """HN collector should return Candidate instances, not dicts."""
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
    assert isinstance(items[0], Candidate)
    assert items[0].source == "hn"
    assert items[0].upvotes == 430


@pytest.mark.asyncio
async def test_reddit_collect_returns_candidate_instances():
    """Reddit collector should return Candidate instances, not dicts."""
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
    assert isinstance(items[0], Candidate)
    assert items[0].source == "reddit"


@pytest.mark.asyncio
async def test_github_collect_returns_candidate_instances():
    """GitHub collector should return Candidate instances, not dicts."""
    from newsbot.collectors import github
    fake_repo = {
        "full_name": "user/repo",
        "html_url": "https://github.com/user/repo",
        "description": "A test repo",
        "stargazers_count": 100,
        "forks_count": 20,
        "created_at": "2026-01-01T00:00:00Z",
        "pushed_at": "2026-07-01T00:00:00Z",
        "topics": [],
    }
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"items": [fake_repo]}
    fake_resp.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.collectors.github.httpx.AsyncClient", return_value=fake_client):
        items = await github.collect({"queries": ["llm"], "limit": 5})

    assert len(items) == 1
    assert isinstance(items[0], Candidate)
    assert items[0].source == "github"
    assert items[0].stars == 100
    assert items[0].penalty == 1.0  # no penalty triggers for this repo


@pytest.mark.asyncio
async def test_producthunt_collect_returns_candidate_instances():
    """ProductHunt collector should return Candidate instances, not dicts."""
    from newsbot.collectors import producthunt
    fake_post = {
        "name": "Cool Product",
        "url": "/posts/cool-product",
        "tagline": "A cool product",
        "votesCount": 500,
        "commentsCount": 50,
        "createdAt": "2026-07-15T10:00:00Z",
        "topics": {"edges": [{"node": {"name": "AI"}}]},
    }
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "data": {"topic": {"posts": {"edges": [{"node": fake_post}]}}}
    }
    fake_resp.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.collectors.producthunt.httpx.AsyncClient", return_value=fake_client):
        with patch.dict("os.environ", {"PH_API_KEY": "test-token"}):
            items = await producthunt.collect({
                "topics": ["ai"], "limit": 5,
            })

    assert len(items) == 1
    assert isinstance(items[0], Candidate)
    assert items[0].source == "producthunt"
    assert items[0].upvotes == 500


@pytest.mark.asyncio
async def test_rss_collect_returns_candidate_instances():
    """RSS collector should return Candidate instances, not dicts."""
    from newsbot.collectors import rss
    entry = {
        "title": "Blog Post",
        "link": "https://blog.example.com/post",
        "summary": "A summary",
        "published": "2026-07-15T10:00:00Z",
    }
    parsed = MagicMock()
    parsed.entries = [entry]
    mock_response = MagicMock()
    mock_response.content = b"<rss>mock</rss>"
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.collectors.rss.httpx.AsyncClient", return_value=mock_client):
        with patch("newsbot.collectors.rss.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=parsed)
            items = await rss.collect({"feeds": [
                {"url": "https://blog.example.com/feed", "name": "Blog", "weight": 1.0}
            ]})

    assert len(items) == 1
    assert isinstance(items[0], Candidate)
    assert items[0].source == "rss"


# --- Downstream integration tests ---------------------------------------

def test_candidate_through_dedupe_and_scoring():
    """Real Candidate objects should pass through dedupe and scoring without errors."""
    from newsbot.dedupe import dedupe_and_merge
    from newsbot.scoring import score_all
    from newsbot.config import DEFAULT_SOURCE_WEIGHTS

    a = new_candidate(
        title="AI Breakthrough", url="https://example.com/ai",
        source="hn", source_name="Hacker News", upvotes=500, comments=100,
    )
    b = new_candidate(
        title="AI Breakthrough", url="https://example.com/ai",
        source="reddit", source_name="r/MachineLearning", upvotes=300, comments=50,
    )
    # Dedupe should merge them
    deduped = dedupe_and_merge([a, b])
    assert len(deduped) == 1
    assert deduped[0]["crosspost_count"] == 2
    assert deduped[0]["upvotes"] == 800

    # Scoring should work on the deduped candidates
    cfg = {"source_weights": DEFAULT_SOURCE_WEIGHTS, "topic_boost": {}, "lookback_hours": 48}
    scored = score_all(deduped, cfg)
    assert len(scored) == 1
    assert scored[0]["score"] > 0


def test_candidate_through_summarizer_payload():
    """Candidate objects should work when summarizer adds fields and serializes."""
    from newsbot.collectors.base import new_candidate

    c = new_candidate(
        title="AI Breakthrough", url="https://example.com/ai",
        source="hn", source_name="Hacker News", upvotes=500,
    )
    # Simulate summarizer adding fields via dict-like access
    c["candidate_id"] = "test_001"
    c["importance"] = 8
    c["reason"] = "Major breakthrough"
    c["short_summary"] = "New method reduces cost by 10x"
    c["extracted_text"] = "Researchers at XYZ lab..."

    # Verify fields are accessible
    assert c["candidate_id"] == "test_001"
    assert c["importance"] == 8
    assert c["reason"] == "Major breakthrough"

    # Verify to_dict includes summarizer fields
    d = c.to_dict()
    assert d["candidate_id"] == "test_001"
    assert d["importance"] == 8
    assert d["reason"] == "Major breakthrough"
    assert d["short_summary"] == "New method reduces cost by 10x"
    assert d["extracted_text"] == "Researchers at XYZ lab..."

    # Verify round-trip through from_dict
    restored = Candidate.from_dict(d)
    assert restored.candidate_id == "test_001"
    assert restored.importance == 8
    assert restored.reason == "Major breakthrough"