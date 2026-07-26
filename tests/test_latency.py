"""Tests for collector and LLM latency bounding (flow_001030)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from newsbot.collectors import github as gh


class TestGitHubConcurrentQueries:
    """Verify GitHub queries run concurrently, not sequentially."""

    @pytest.mark.asyncio
    async def test_queries_run_concurrently(self):
        """Multiple queries should execute concurrently."""
        call_times = []

        async def mock_fetch_one(client, *, query, limit, sort):
            call_times.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.1)  # simulate network delay
            return [{"title": f"repo-{query}", "url": f"https://github.com/{query}", "source": "github"}]

        config = {"queries": ["llm", "agent", "unity"], "limit": 5}
        with patch("newsbot.collectors.github._fetch_one", side_effect=mock_fetch_one):
            results = await gh.collect(config)

        # If sequential: 3 × 0.1s = 0.3s. If concurrent: ~0.1s.
        assert len(results) == 3
        # All three calls should start within ~0.05s of each other (concurrent).
        if len(call_times) >= 2:
            spread = max(call_times) - min(call_times)
            assert spread < 0.08, f"Queries ran sequentially (spread={spread:.3f}s)"

    @pytest.mark.asyncio
    async def test_partial_failure_does_not_discard_others(self):
        """If one query fails, results from others should still be returned."""
        async def mock_fetch_one(client, *, query, limit, sort):
            if query == "fail":
                raise RuntimeError("network error")
            return [{"title": f"repo-{query}", "url": f"https://github.com/{query}", "source": "github"}]

        config = {"queries": ["llm", "fail", "unity"], "limit": 5}
        with patch("newsbot.collectors.github._fetch_one", side_effect=mock_fetch_one):
            results = await gh.collect(config)

        # Two successful queries, one failed.
        assert len(results) == 2
        titles = [r["title"] for r in results]
        assert "repo-llm" in titles
        assert "repo-unity" in titles


class TestLLMRetryBudget:
    """Verify LLM retry budget is bounded."""

    def test_max_retries_default_is_3(self):
        """Default max_retries should be 3, not 5 (was 5 before fix)."""
        from lm_client import LMClient
        client = LMClient("http://localhost", "model", 30.0)
        assert client.max_retries == 3

    def test_max_retries_configurable(self):
        from lm_client import LMClient
        client = LMClient("http://localhost", "model", 30.0, max_retries=1)
        assert client.max_retries == 1

    @pytest.mark.asyncio
    async def test_llm_gives_up_after_max_retries(self):
        """LLM should raise after max_retries, not retry indefinitely."""
        from lm_client import LMClient, LLMTransientError
        import httpx

        client = LMClient("http://localhost:99999", "model", 0.1, max_retries=2)

        # Mock httpx to always timeout
        async def mock_post(*args, **kwargs):
            raise httpx.ConnectTimeout("connection timed out")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with pytest.raises(LLMTransientError):
                await client.generate([{"role": "user", "content": "hi"}])