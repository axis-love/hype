"""Tests for the HN and Reddit collectors — engagement signal capture.

HN uses httpx (Algolia API). Reddit uses the JSON API (oauth.reddit.com).
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


# --- Reddit (batched multi-subreddit JSON API) ----------------------------

def _reddit_json_child(
    title: str = "GTA 6 gameplay leaks online",
    permalink: str = "/r/GamingLeaksAndRumours/comments/abc/gta_6_leaks/",
    subreddit: str = "GamingLeaksAndRumours",
    score: int = 21675,
    num_comments: int = 1113,
    created_utc: float = 1723795200.0,  # 2024-08-16T08:00:00Z
    over_18: bool = False,
    selftext: str = "",
    external_url: str = "https://example.com/leak",
    preview: dict | None = None,
    is_self: bool = False,
) -> dict[str, Any]:
    """Create a fake Reddit JSON API child entry."""
    return {
        "kind": "t3",
        "data": {
            "title": title,
            "permalink": permalink,
            "subreddit": subreddit,
            "score": score,
            "num_comments": num_comments,
            "created_utc": created_utc,
            "over_18": over_18,
            "selftext": selftext,
            "url": external_url,
            "preview": preview,
            "is_self": is_self,
        },
    }


def _reddit_json_response(children: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a fake Reddit JSON API response."""
    return {"data": {"children": children}}


def _mock_httpx_client_get(
    responses: list[MagicMock] | MagicMock,
) -> AsyncMock:
    """A fake httpx.AsyncClient whose GET returns *responses* (sequentially
    if list) or a single response.
    """
    client = AsyncMock()
    if isinstance(responses, list):
        client.get = AsyncMock(side_effect=list(responses))
    else:
        client.get = AsyncMock(return_value=responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _ok_json_response(children: list[dict[str, Any]]) -> MagicMock:
    """200 response with JSON body for the Reddit API."""
    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.json.return_value = _reddit_json_response(children)
    return response


def _ok_token_response() -> MagicMock:
    """200 response for the token endpoint."""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "access_token": "fake-jwt-token",
        "token_type": "bearer",
        "expires_in": 86400,
        "scope": "*",
    }
    return response


def _throttled_response(retry_after: str = "2") -> MagicMock:
    """429 response."""
    response = MagicMock()
    response.status_code = 429
    response.headers = {"Retry-After": retry_after}
    response.json.return_value = {}
    return response


def _unauthorized_response() -> MagicMock:
    """401 response."""
    response = MagicMock()
    response.status_code = 401
    response.headers = {}
    response.json.return_value = {}
    return response


@pytest.fixture(autouse=True)
def _reset_token_cache():
    """Clear the module-level token cache before each test."""
    reddit._access_token_cache.clear()
    yield
    reddit._access_token_cache.clear()


def _patch_token_and_api(monkeypatch, token_resp=None, api_responses=None):
    """Patch httpx.AsyncClient to return token then API responses."""
    token_resp = token_resp or _ok_token_response()
    if api_responses is None:
        api_responses = [_ok_json_response([])]
    elif isinstance(api_responses, MagicMock):
        api_responses = [api_responses]

    call_count = [0]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *args, **kwargs):
            return token_resp

        async def get(self, *args, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(api_responses):
                return api_responses[idx]
            return api_responses[-1]

    fake_client = FakeClient()
    monkeypatch.setattr(reddit.httpx, "AsyncClient", lambda *a, **kw: fake_client)
    return fake_client


@pytest.mark.asyncio
async def test_reddit_collect_captures_real_score_and_comments(monkeypatch):
    """Reddit JSON API returns real upvotes (score) and comments."""
    child = _reddit_json_child()
    _patch_token_and_api(monkeypatch, api_responses=[_ok_json_response([child])])
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")
    items = await reddit.collect({"subreddits": ["GamingLeaksAndRumours"], "limit": 10})

    assert len(items) == 1
    assert items[0]["source"] == "reddit"
    assert items[0]["source_name"] == "r/GamingLeaksAndRumours"
    assert items[0]["upvotes"] == 21675
    assert items[0]["comments"] == 1113
    assert items[0]["url"] == "https://www.reddit.com/r/GamingLeaksAndRumours/comments/abc/gta_6_leaks/"


@pytest.mark.asyncio
async def test_reddit_collect_empty_response(monkeypatch):
    """Empty JSON response should return empty list."""
    _patch_token_and_api(monkeypatch, api_responses=[_ok_json_response([])])
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")
    items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})
    assert items == []


@pytest.mark.asyncio
async def test_reddit_collect_multiple_subreddits_one_batched_request(monkeypatch):
    """Two subs fit in one batch: ONE API request, correct sub attribution."""
    child1 = _reddit_json_child(
        title="Story 1", subreddit="LocalLLaMA",
        permalink="/r/LocalLLaMA/comments/1/s1/", score=100, num_comments=10,
    )
    child2 = _reddit_json_child(
        title="Story 2", subreddit="MachineLearning",
        permalink="/r/MachineLearning/comments/2/s2/", score=200, num_comments=20,
    )
    client = _patch_token_and_api(
        monkeypatch,
        api_responses=[_ok_json_response([child1, child2])],
    )
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")
    items = await reddit.collect({"subreddits": ["LocalLLaMA", "MachineLearning"], "limit": 10})

    assert len(items) == 2
    assert items[0]["source_name"] == "r/LocalLLaMA"
    assert items[1]["source_name"] == "r/MachineLearning"
    assert items[0]["upvotes"] == 100
    assert items[1]["upvotes"] == 200


@pytest.mark.asyncio
async def test_reddit_grouping_12_subs_batch_4_makes_3_requests(monkeypatch):
    """12 subs / batch size 4 = exactly 3 sequential API requests."""
    monkeypatch.delenv("NEWS_REDDIT_BATCH_SIZE", raising=False)
    subs = [f"sub{i}" for i in range(1, 13)]
    client = _patch_token_and_api(
        monkeypatch,
        api_responses=[
            _ok_json_response([]),
            _ok_json_response([]),
            _ok_json_response([]),
        ],
    )
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with patch("newsbot.collectors.reddit._sleep", side_effect=fake_sleep):
        items = await reddit.collect({"subreddits": subs, "limit": 10})

    assert items == []
    # Inter-group pacing between the 3 sequential fetches.
    assert sleep_calls == [2.0, 2.0]


@pytest.mark.asyncio
async def test_reddit_nsfw_dropped(monkeypatch):
    """over_18 (NSFW) entries are dropped."""
    nsfw_child = _reddit_json_child(title="NSFW Post", over_18=True,
                                     subreddit="LocalLLaMA",
                                     permalink="/r/LocalLLaMA/comments/1/nsfw/")
    safe_child = _reddit_json_child(title="Safe Post", over_18=False,
                                     subreddit="LocalLLaMA",
                                     permalink="/r/LocalLLaMA/comments/2/safe/")
    _patch_token_and_api(
        monkeypatch,
        api_responses=[_ok_json_response([nsfw_child, safe_child])],
    )
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")
    items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert len(items) == 1
    assert items[0]["title"] == "Safe Post"


@pytest.mark.asyncio
async def test_reddit_attribution_mixed_response(monkeypatch):
    """A mixed multi-sub response is split into correct source_name per entry."""
    children = [
        _reddit_json_child(title="A1", subreddit="LocalLLaMA",
                            permalink="/r/LocalLLaMA/comments/1/a1/", score=10),
        _reddit_json_child(title="B1", subreddit="MachineLearning",
                            permalink="/r/MachineLearning/comments/2/b1/", score=20),
        _reddit_json_child(title="A2", subreddit="LocalLLaMA",
                            permalink="/r/LocalLLaMA/comments/3/a2/", score=30),
    ]
    _patch_token_and_api(monkeypatch, api_responses=[_ok_json_response(children)])
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")
    items = await reddit.collect({"subreddits": ["LocalLLaMA", "MachineLearning"], "limit": 10})

    assert [i["source_name"] for i in items] == [
        "r/LocalLLaMA", "r/MachineLearning", "r/LocalLLaMA",
    ]


@pytest.mark.asyncio
async def test_reddit_per_sub_limit_applied_after_attribution(monkeypatch):
    """The per-subreddit cap is applied per attributed sub, not per response."""
    children = [
        _reddit_json_child(title="A1", subreddit="Alpha",
                            permalink="/r/Alpha/comments/1/a1/"),
        _reddit_json_child(title="A2", subreddit="Alpha",
                            permalink="/r/Alpha/comments/2/a2/"),
        _reddit_json_child(title="A3", subreddit="Alpha",
                            permalink="/r/Alpha/comments/3/a3/"),
        _reddit_json_child(title="B1", subreddit="Beta",
                            permalink="/r/Beta/comments/4/b1/"),
    ]
    _patch_token_and_api(monkeypatch, api_responses=[_ok_json_response(children)])
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")
    items = await reddit.collect({"subreddits": ["Alpha", "Beta"], "limit": 2})

    assert [i["title"] for i in items] == ["A1", "A2", "B1"]
    assert [i["source_name"] for i in items] == ["r/Alpha", "r/Alpha", "r/Beta"]


@pytest.mark.asyncio
async def test_reddit_unconfigured_sub_in_response_is_dropped(monkeypatch):
    """Reddit may return entries for unconfigured subs — drop those."""
    children = [
        _reddit_json_child(title="A1", subreddit="Alpha",
                            permalink="/r/Alpha/comments/1/a1/"),
        _reddit_json_child(title="Stranger", subreddit="Unknown",
                            permalink="/r/Unknown/comments/2/x/"),
        _reddit_json_child(title="E1", subreddit="Epsilon",
                            permalink="/r/Epsilon/comments/3/e1/"),
    ]
    _patch_token_and_api(monkeypatch, api_responses=[_ok_json_response(children)])
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")
    items = await reddit.collect({"subreddits": ["Alpha", "Epsilon"], "limit": 10})

    assert [i["source_name"] for i in items] == ["r/Alpha", "r/Epsilon"]


@pytest.mark.asyncio
async def test_reddit_429_retries_once_then_succeeds(monkeypatch):
    """A 429 with Retry-After sleeps min(header, 30) and retries the group once."""
    api_responses = [_throttled_response("2"), _ok_json_response([
        _reddit_json_child(title="Recovered",
                           subreddit="LocalLLaMA",
                           permalink="/r/LocalLLaMA/comments/9/r/")
    ])]
    _patch_token_and_api(monkeypatch, api_responses=api_responses)
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with patch("newsbot.collectors.reddit._sleep", side_effect=fake_sleep):
        items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert sleep_calls == [2.0]
    assert len(items) == 1
    assert items[0]["title"] == "Recovered"


@pytest.mark.asyncio
async def test_reddit_429_twice_returns_empty(monkeypatch):
    """Both attempts throttled -> [] for the group, no exception raised."""
    throttled = _throttled_response("2")
    _patch_token_and_api(
        monkeypatch,
        api_responses=[throttled, throttled],
    )
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")

    async def fake_sleep(seconds: float) -> None:
        pass

    with patch("newsbot.collectors.reddit._sleep", side_effect=fake_sleep):
        items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert items == []


@pytest.mark.asyncio
async def test_reddit_401_refreshes_token_and_retries(monkeypatch):
    """A 401 invalidates the token cache and retries the group once."""
    unauthorized = _unauthorized_response()
    ok = _ok_json_response([_reddit_json_child(title="After Refresh",
                                                  subreddit="LocalLLaMA",
                                                  permalink="/r/LocalLLaMA/comments/1/after/")])
    _patch_token_and_api(
        monkeypatch,
        api_responses=[unauthorized, ok],
    )
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")

    items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert len(items) == 1
    assert items[0]["title"] == "After Refresh"
    # The token cache was repopulated by the retry (proves the refresh path ran).
    assert reddit._access_token_cache.get("token") == "fake-jwt-token"


@pytest.mark.asyncio
async def test_reddit_no_refresh_token_returns_empty(monkeypatch):
    """No REDDIT_REFRESH_TOKEN env -> [] and no network activity."""
    monkeypatch.delenv("REDDIT_REFRESH_TOKEN", raising=False)
    items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})
    assert items == []


@pytest.mark.asyncio
async def test_reddit_empty_config_returns_empty_without_http(monkeypatch):
    """No configured subreddits -> [] and no network activity at all."""
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")
    assert await reddit.collect({}) == []
    assert await reddit.collect({"subreddits": []}) == []
    assert await reddit.collect({"subreddits": ["  ", "/"]}) == []


@pytest.mark.asyncio
async def test_reddit_self_post_kept(monkeypatch):
    """Self-posts (discussion threads) are kept, not dropped."""
    child = _reddit_json_child(
        title="Weekly Discussion Thread",
        subreddit="LocalLLaMA",
        permalink="/r/LocalLLaMA/comments/abc/disc/",
        selftext="This is a self post body",
        is_self=True,
        external_url="https://www.reddit.com/r/LocalLLaMA/comments/abc/disc/",
    )
    _patch_token_and_api(monkeypatch, api_responses=[_ok_json_response([child])])
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")
    items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert len(items) == 1
    assert items[0]["title"] == "Weekly Discussion Thread"


@pytest.mark.asyncio
async def test_reddit_raw_json_has_external_url_and_preview(monkeypatch):
    """raw_json stores the external link and preview for the media extractor."""
    preview = {"images": [{"source": {"url": "https://preview.com/img.jpg"}}]}
    child = _reddit_json_child(
        title="Leak with image",
        subreddit="GamingLeaksAndRumours",
        external_url="https://example.com/article",
        preview=preview,
    )
    _patch_token_and_api(monkeypatch, api_responses=[_ok_json_response([child])])
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")
    items = await reddit.collect({"subreddits": ["GamingLeaksAndRumours"], "limit": 10})

    assert len(items) == 1
    rj = items[0]["raw_json"]
    assert rj["external_url"] == "https://example.com/article"
    assert rj["preview"] == preview


@pytest.mark.asyncio
async def test_reddit_collect_returns_candidate_instances(monkeypatch):
    """Reddit collector should return Candidate instances, not dicts."""
    child = _reddit_json_child(subreddit="LocalLLaMA",
                               permalink="/r/LocalLLaMA/comments/abc/test/")
    _patch_token_and_api(monkeypatch, api_responses=[_ok_json_response([child])])
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")
    items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    from newsbot.collectors.base import Candidate
    assert len(items) == 1
    assert isinstance(items[0], Candidate)
    assert items[0].source == "reddit"


@pytest.mark.asyncio
async def test_reddit_published_at_from_created_utc(monkeypatch):
    """published_at is derived from created_utc epoch seconds."""
    # 1723795200 = 2024-08-16T08:00:00Z
    child = _reddit_json_child(subreddit="LocalLLaMA",
                               permalink="/r/LocalLLaMA/comments/abc/time/",
                               created_utc=1723795200.0)
    _patch_token_and_api(monkeypatch, api_responses=[_ok_json_response([child])])
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "test-refresh-token")
    items = await reddit.collect({"subreddits": ["LocalLLaMA"], "limit": 10})

    assert len(items) == 1
    assert items[0]["published_at"].startswith("2024-08-16")


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


# --- Google Trends collector (H-3) -------------------------------------

from newsbot.collectors import trends as trends_collector


def _trends_rss_xml(
    items: list[dict[str, Any]] | None = None,
    traffic: str = "200+",
    published: str = "Sat, 22 Aug 2026 05:00:00 -0700",
) -> bytes:
    """Build a real Google Trends RSS document from item specs.

    Each item spec: {"title": str, "news": [(title, url, source), ...],
    "traffic": str (optional, defaults to *traffic*)}. Rendering real XML
    (instead of mocking feedparser) exercises the ElementTree parser the
    collector actually uses — feedparser collapses the repeated
    ht:news_item elements into one string, so those fixtures encoded the
    wrong assumption.
    """
    if items is None:
        items = [{
            "title": "marcus rashford",
            "news": [
                ("Article about marcus rashford",
                 "https://example.com/marcus-rashford", "The Sun"),
            ],
        }]

    ns = trends_collector._HT_NS
    body: list[str] = []
    for spec in items:
        t = spec.get("traffic", traffic)
        body.append("    <item>\n")
        body.append(f"      <title>{spec['title']}</title>\n")
        body.append(f"      <ht:approx_traffic xmlns:ht=\"{ns}\">{t}</ht:approx_traffic>\n")
        body.append(f"      <pubDate>{published}</pubDate>\n")
        for ntitle, nurl, nsrc in spec.get("news", []):
            body.append(f"      <ht:news_item xmlns:ht=\"{ns}\">\n")
            body.append(f"        <ht:news_item_title xmlns:ht=\"{ns}\">{ntitle}</ht:news_item_title>\n")
            body.append(f"        <ht:news_item_url xmlns:ht=\"{ns}\">{nurl}</ht:news_item_url>\n")
            body.append(f"        <ht:news_item_source xmlns:ht=\"{ns}\">{nsrc}</ht:news_item_source>\n")
            body.append("      </ht:news_item>\n")
        body.append("    </item>\n")

    return (
        "<rss version=\"2.0\">\n  <channel>\n"
        + "".join(body)
        + "  </channel>\n</rss>\n"
    ).encode("utf-8")


def _mock_trends_http(content: bytes) -> AsyncMock:
    """AsyncClient mock whose .get() returns *content*."""
    mock_response = MagicMock()
    mock_response.content = content
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


@pytest.mark.asyncio
async def test_trends_collect_captures_traffic_and_news():
    """Trends RSS → candidates with correct traffic→reposts mapping."""
    xml = _trends_rss_xml(items=[{
        "title": "GTA 6 leak",
        "traffic": "200+",
        "news": [
            ("GTA 6 gameplay leaks online ahead of release",
             "https://www.ign.com/articles/gta-6-leak", "IGN"),
        ],
    }])
    mock_client = _mock_trends_http(xml)

    with patch("newsbot.collectors.trends.httpx.AsyncClient", return_value=mock_client):
        items = await trends_collector.collect({"geos": ["US"], "limit": 3})

    assert len(items) == 1
    assert items[0]["source"] == "trends"
    assert items[0]["source_name"] == "trends/GTA 6 leak"
    assert items[0]["title"] == "GTA 6 gameplay leaks online ahead of release"
    assert items[0]["url"] == "https://www.ign.com/articles/gta-6-leak"
    assert items[0]["reposts"] == 200  # 200+ → 200


@pytest.mark.asyncio
async def test_trends_collect_yields_all_three_news_links():
    """feedparser collapsed the 3 ht:news_item per trend into one string —
    the collector must yield one candidate per news link (up to 3)."""
    xml = _trends_rss_xml(items=[{
        "title": "nfl preseason",
        "traffic": "2000+",
        "news": [
            ("NFL preseason week 3 recap", "https://a.com/1", "ESPN"),
            ("Top plays from Friday night", "https://b.com/2", "NFL.com"),
            ("Injury report after preseason", "https://c.com/3", "CBS"),
        ],
    }])
    mock_client = _mock_trends_http(xml)

    with patch("newsbot.collectors.trends.httpx.AsyncClient", return_value=mock_client):
        items = await trends_collector.collect({"geos": ["US"]})

    assert len(items) == 3
    assert [c["url"] for c in items] == [
        "https://a.com/1", "https://b.com/2", "https://c.com/3",
    ]
    assert [c["title"] for c in items] == [
        "NFL preseason week 3 recap",
        "Top plays from Friday night",
        "Injury report after preseason",
    ]
    assert all(c["source_name"] == "trends/nfl preseason" for c in items)


@pytest.mark.asyncio
async def test_trends_traffic_mapping_all_values():
    """All traffic string values map to correct reposts."""
    test_cases = [
        ("200+", 200), ("500+", 500), ("1000+", 1000),
        ("2000+", 2000), ("5000+", 5000), ("10000+", 10000),
        ("Breakout", 5000), ("breakout", 5000),
        ("", 0), (None, 0), ("unknown", 0),
    ]
    for traffic, expected in test_cases:
        assert trends_collector._traffic_to_reposts(traffic) == expected


@pytest.mark.asyncio
async def test_trends_multiple_news_items_capped_at_3():
    """Each trend produces at most 3 candidates (one per news link)."""
    xml = _trends_rss_xml(items=[{
        "title": "trending topic",
        "traffic": "1000+",
        "news": [
            ("News 1", "https://a.com/1", "A"),
            ("News 2", "https://b.com/2", "B"),
            ("News 3", "https://c.com/3", "C"),
            ("News 4", "https://d.com/4", "D"),
        ],
    }])
    mock_client = _mock_trends_http(xml)

    with patch("newsbot.collectors.trends.httpx.AsyncClient", return_value=mock_client):
        items = await trends_collector.collect({"geos": ["US"], "limit": 3})

    assert len(items) == 3  # capped at 3
    assert items[0]["title"] == "News 1"
    assert items[2]["title"] == "News 3"


@pytest.mark.asyncio
async def test_trends_empty_feed():
    """Empty RSS feed should return empty list."""
    mock_client = _mock_trends_http(b"<rss><channel></channel></rss>")

    with patch("newsbot.collectors.trends.httpx.AsyncClient", return_value=mock_client):
        items = await trends_collector.collect({"geos": ["US"]})

    assert items == []


@pytest.mark.asyncio
async def test_trends_empty_geos_returns_empty():
    """Empty geos list -> [] and no network activity."""
    items = await trends_collector.collect({"geos": []})
    assert items == []


@pytest.mark.asyncio
async def test_trends_multiple_geos():
    """Multiple geos produce multiple fetches."""
    mock_client = _mock_trends_http(b"<rss><channel></channel></rss>")

    with patch("newsbot.collectors.trends.httpx.AsyncClient", return_value=mock_client):
        await trends_collector.collect({"geos": ["US", "GB", "JP"]})

    assert mock_client.get.call_count == 3
    urls = [call.args[0] for call in mock_client.get.call_args_list]
    assert "geo=US" in urls[0]
    assert "geo=GB" in urls[1]
    assert "geo=JP" in urls[2]


@pytest.mark.asyncio
async def test_trends_collect_returns_candidate_instances():
    """Trends collector should return Candidate instances."""
    xml = _trends_rss_xml()
    mock_client = _mock_trends_http(xml)

    with patch("newsbot.collectors.trends.httpx.AsyncClient", return_value=mock_client):
        items = await trends_collector.collect({"geos": ["US"]})

    from newsbot.collectors.base import Candidate
    assert len(items) == 1
    assert isinstance(items[0], Candidate)
    assert items[0].source == "trends"


@pytest.mark.asyncio
async def test_trends_raw_json_has_trend_title():
    """raw_json stores trend_title and traffic for dedupe/audit."""
    xml = _trends_rss_xml(items=[{
        "title": "GTA 6",
        "traffic": "Breakout",
        "news": [("GTA 6 news", "https://example.com/gta6", "Kotaku")],
    }])
    mock_client = _mock_trends_http(xml)

    with patch("newsbot.collectors.trends.httpx.AsyncClient", return_value=mock_client):
        items = await trends_collector.collect({"geos": ["US"]})

    assert len(items) == 1
    rj = items[0]["raw_json"]
    assert rj["trend_title"] == "GTA 6"
    assert rj["traffic"] == "Breakout"
    assert rj["geo"] == "US"