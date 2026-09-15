"""H14 R11: shared HTTP clients."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lm_client import LMClient
from newsbot.generation import collect_all


@pytest.mark.asyncio
async def test_collect_all_opens_one_http_client():
    created = {"n": 0}
    real_cls = __import__("httpx").AsyncClient

    class CountingClient(real_cls):
        def __init__(self, *a, **k):
            created["n"] += 1
            super().__init__(*a, **k)

    async def fake_collect(config, client=None):
        assert client is not None
        return []

    cfg = {"sources": {"hackernews": {"limit": 1}, "rss": {"feeds": []}}}
    with patch("httpx.AsyncClient", CountingClient), \
         patch("newsbot.collectors.hackernews.collect", fake_collect), \
         patch("newsbot.collectors.rss.collect", fake_collect), \
         patch("newsbot.generation.COLLECTORS", {
             "hackernews": MagicMock(collect=fake_collect),
             "rss": MagicMock(collect=fake_collect),
         }):
        await collect_all(cfg)
    assert created["n"] == 1


@pytest.mark.asyncio
async def test_lmclient_reuses_one_http_client():
    created = {"n": 0}

    class FakeResp:
        content = b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    class FakeClient:
        is_closed = False
        def __init__(self, *a, **k):
            created["n"] += 1
        async def post(self, *a, **k):
            return FakeResp()
        async def aclose(self):
            self.is_closed = True

    with patch("httpx.AsyncClient", FakeClient):
        lm = LMClient("http://x", "m", 5.0)
        await lm.generate([{"role": "user", "content": "a"}])
        await lm.generate([{"role": "user", "content": "b"}])
        await lm.aclose()
    assert created["n"] == 1


@pytest.mark.asyncio
async def test_github_and_rss_pass_timeout_and_headers_on_shared_client(monkeypatch):
    recorded: list[tuple[str, dict]] = []

    class FakeResp:
        status_code = 200
        content = b""

        def json(self):
            return {"items": [], "hits": []}

    class FakeClient:
        async def get(self, url, **kwargs):
            recorded.append((str(url), kwargs))
            return FakeResp()

    from newsbot.collectors import github, rss

    monkeypatch.setenv("GITHUB_TOKEN", "gh_test_token")
    client = FakeClient()
    await github.collect({"queries": ["llm"], "limit": 1}, client)
    await rss.collect({"feeds": [{"name": "x", "url": "https://example.com/feed.xml"}]}, client)

    gh = [kw for url, kw in recorded if "api.github.com" in url]
    assert gh, recorded
    headers = gh[0].get("headers") or {}
    assert headers.get("Authorization") == "Bearer gh_test_token"
    assert headers.get("Accept") == "application/vnd.github+json"
    assert headers.get("User-Agent") == "newsbot/0.1"
    assert gh[0].get("timeout") is not None

    rss_hits = [kw for url, kw in recorded if "example.com/feed.xml" in url]
    assert rss_hits, recorded
    assert rss_hits[0].get("timeout") is not None

