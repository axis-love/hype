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
