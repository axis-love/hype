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


# --- Reddit (batched multi-subreddit RSS) --------------------------------

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


def _mock_httpx_client(response: MagicMock) -> AsyncMock:
    """A fake httpx.AsyncClient whose GET returns *response*."""
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _ok_response(content: bytes = b"<rss>mock</rss>") -> MagicMock:
    response = MagicMock()
    response.content = content
    response.status_code = 200
    response.headers = {}
    return response


@pytest.mark.asyncio
async def test_reddit_collect_captures_score_and_comments_from_rss():
    """Reddit collector uses RSS; engagement is parsed from entry title."""
    entry = _reddit_rss_entry()
    parsed = _make_parsed_feed([entry])

    with patch("newsbot.collectors.reddit.httpx.AsyncClient",
               return_value=_mock_httpx_client(_ok_response())):
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

    with patch("newsbot.collectors.reddit.httpx.AsyncClient",
               return_value=_mock_httpx_client(_ok_response())):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=parsed)
            items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert items == []


@pytest.mark.asyncio
async def test_reddit_collect_handles_missing_engagement():
    """Reddit RSS entry without vote/comment counts in title."""
    entry = _reddit_rss_entry(title="Just a title without counts")
    parsed = _make_parsed_feed([entry])

    with patch("newsbot.collectors.reddit.httpx.AsyncClient",
               return_value=_mock_httpx_client(_ok_response())):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=parsed)
            items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert len(items) == 1
    assert items[0]["upvotes"] is None  # no count in title
    assert items[0]["comments"] is None


@pytest.mark.asyncio
async def test_reddit_collect_multiple_subreddits_one_batched_request():
    """Two subs fit in one batch: ONE request, attribution from permalinks."""
    entry1 = _reddit_rss_entry(title="Story 1 — 100 votes, 10 comments")
    entry2 = _reddit_rss_entry(title="Story 2 — 200 votes, 20 comments",
                               link="https://www.reddit.com/r/MachineLearning/comments/xyz/story2/")
    parsed = _make_parsed_feed([entry1, entry2])

    client = _mock_httpx_client(_ok_response())
    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=client):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=parsed)
            items = await reddit.collect({"subreddits": ["LocalLLaMA", "MachineLearning"], "limit": 10})

    assert client.get.call_count == 1
    url = client.get.call_args.args[0]
    assert "/r/LocalLLaMA+MachineLearning/hot.rss" in url
    assert len(items) == 2
    assert items[0]["source_name"] == "r/LocalLLaMA"
    assert items[1]["source_name"] == "r/MachineLearning"


@pytest.mark.asyncio
async def test_reddit_grouping_12_subs_batch_4_makes_3_requests(monkeypatch):
    """12 subs / batch size 4 = exactly 3 sequential requests, a+b+c+d URLs."""
    monkeypatch.delenv("NEWS_REDDIT_BATCH_SIZE", raising=False)
    subs = [f"sub{i}" for i in range(1, 13)]
    client = _mock_httpx_client(_ok_response())
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=client):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=_make_parsed_feed([]))
            with patch("newsbot.collectors.reddit._sleep", side_effect=fake_sleep):
                items = await reddit.collect({"subreddits": subs, "limit": 10})

    assert items == []
    assert client.get.call_count == 3
    urls = [call.args[0] for call in client.get.call_args_list]
    assert urls[0] == "https://www.reddit.com/r/sub1+sub2+sub3+sub4/hot.rss?limit=40"
    assert urls[1] == "https://www.reddit.com/r/sub5+sub6+sub7+sub8/hot.rss?limit=40"
    assert urls[2] == "https://www.reddit.com/r/sub9+sub10+sub11+sub12/hot.rss?limit=40"
    # Inter-group pacing between the 3 sequential fetches.
    assert sleep_calls == [2.0, 2.0]


@pytest.mark.asyncio
async def test_reddit_batch_limit_capped_at_100():
    """?limit= is group_size * per_sub_limit, capped at Reddit's max of 100."""
    client = _mock_httpx_client(_ok_response())
    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=client):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=_make_parsed_feed([]))
            await reddit.collect({"subreddits": ["a", "b", "c", "d"], "limit": 25})

    assert client.get.call_args.args[0].endswith("?limit=100")  # 4 * 25 = 100


@pytest.mark.asyncio
async def test_reddit_attribution_mixed_response():
    """A mixed multi-sub response is split into correct source_name per entry."""
    entries = [
        _reddit_rss_entry(title="A1 — 10 votes, 1 comments",
                          link="https://www.reddit.com/r/LocalLLaMA/comments/1/a1/"),
        _reddit_rss_entry(title="B1 — 20 votes, 2 comments",
                          link="https://www.reddit.com/r/MachineLearning/comments/2/b1/"),
        _reddit_rss_entry(title="A2 — 30 votes, 3 comments",
                          link="https://www.reddit.com/r/LocalLLaMA/comments/3/a2/"),
    ]
    client = _mock_httpx_client(_ok_response())
    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=client):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=_make_parsed_feed(entries))
            items = await reddit.collect({"subreddits": ["LocalLLaMA", "MachineLearning"], "limit": 10})

    assert [i["source_name"] for i in items] == [
        "r/LocalLLaMA", "r/MachineLearning", "r/LocalLLaMA",
    ]


@pytest.mark.asyncio
async def test_reddit_per_sub_limit_applied_after_attribution():
    """The per-subreddit cap is applied per attributed sub, not per response."""
    entries = [
        _reddit_rss_entry(title="A1", link="https://www.reddit.com/r/Alpha/comments/1/a1/"),
        _reddit_rss_entry(title="A2", link="https://www.reddit.com/r/Alpha/comments/2/a2/"),
        _reddit_rss_entry(title="A3", link="https://www.reddit.com/r/Alpha/comments/3/a3/"),
        _reddit_rss_entry(title="B1", link="https://www.reddit.com/r/Beta/comments/4/b1/"),
    ]
    client = _mock_httpx_client(_ok_response())
    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=client):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=_make_parsed_feed(entries))
            items = await reddit.collect({"subreddits": ["Alpha", "Beta"], "limit": 2})

    assert [i["title"] for i in items] == ["A1", "A2", "B1"]
    assert [i["source_name"] for i in items] == ["r/Alpha", "r/Alpha", "r/Beta"]


@pytest.mark.asyncio
async def test_reddit_unconfigured_sub_in_response_is_dropped():
    """Reddit may leak unlisted subs into a group feed — drop those entries,
    but keep entries for a configured sub that belongs to another group."""
    entries = [
        _reddit_rss_entry(title="A1", link="https://www.reddit.com/r/Alpha/comments/1/a1/"),
        _reddit_rss_entry(title="Stranger", link="https://www.reddit.com/r/Unknown/comments/2/x/"),
        # Configured sub from a DIFFERENT group (Reddit does this): keep it.
        _reddit_rss_entry(title="E1", link="https://www.reddit.com/r/Epsilon/comments/3/e1/"),
    ]
    client = _mock_httpx_client(_ok_response())
    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=client):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=_make_parsed_feed(entries))
            items = await reddit.collect({"subreddits": ["Alpha", "Epsilon"], "limit": 10})

    assert [i["source_name"] for i in items] == ["r/Alpha", "r/Epsilon"]


@pytest.mark.asyncio
async def test_reddit_429_with_retry_after_retries_once_then_succeeds():
    """A 429 with Retry-After sleeps min(header, 30) and retries the group once."""
    throttled = MagicMock()
    throttled.content = b""
    throttled.status_code = 429
    throttled.headers = {"Retry-After": "2"}

    ok = _ok_response()
    client = AsyncMock()
    client.get = AsyncMock(side_effect=[throttled, ok])
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    entry = _reddit_rss_entry(title="Recovered", link="https://www.reddit.com/r/LocalLLaMA/comments/9/r/")
    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=client):
        with patch("newsbot.collectors.reddit.feedparser") as mock_fp:
            mock_fp.parse = MagicMock(return_value=_make_parsed_feed([entry]))
            with patch("newsbot.collectors.reddit._sleep", side_effect=fake_sleep):
                items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert client.get.call_count == 2
    assert sleep_calls == [2.0]
    assert len(items) == 1
    assert items[0]["title"] == "Recovered"


@pytest.mark.asyncio
async def test_reddit_429_twice_returns_empty_without_exception():
    """Both attempts throttled -> [] for the group, no exception raised."""
    throttled = MagicMock()
    throttled.content = b""
    throttled.status_code = 429
    throttled.headers = {"Retry-After": "2"}

    client = AsyncMock()
    client.get = AsyncMock(return_value=throttled)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    async def fake_sleep(seconds: float) -> None:
        pass

    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=client):
        with patch("newsbot.collectors.reddit._sleep", side_effect=fake_sleep):
            items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert items == []
    assert client.get.call_count == 2  # initial + exactly one retry


@pytest.mark.asyncio
async def test_reddit_empty_config_returns_empty_without_http():
    """No configured subreddits -> [] and no network activity at all."""
    client = _mock_httpx_client(_ok_response())
    with patch("newsbot.collectors.reddit.httpx.AsyncClient", return_value=client):
        assert await reddit.collect({}) == []
        assert await reddit.collect({"subreddits": []}) == []
        assert await reddit.collect({"subreddits": ["  ", "/"]}) == []
    assert client.get.call_count == 0


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