"""Tests for publication metadata and documentation correctness (flow_001034)."""
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

import pytest

from newsbot.collectors import github as gh


class TestGitHubPublicationMetadata:
    """Verify GitHub uses created_at (repo creation), not pushed_at (last activity)."""

    def _github_repo(self, **overrides) -> dict[str, Any]:
        base = {
            "full_name": "owner/repo",
            "html_url": "https://github.com/owner/repo",
            "description": "A cool project",
            "stargazers_count": 5000,
            "forks_count": 500,
            "topics": ["ai"],
            "created_at": "2024-01-01T00:00:00Z",
            "pushed_at": "2026-07-26T10:00:00Z",
            "updated_at": "2026-07-26T10:00:00Z",
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_published_at_uses_created_at_not_pushed_at(self):
        """GitHub candidates should use created_at as published_at, not pushed_at."""
        repo = self._github_repo()
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"items": [repo]}
        fake_resp.raise_for_status = MagicMock()

        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=fake_resp)
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        with patch("newsbot.collectors.github.httpx.AsyncClient", return_value=fake_client):
            items = await gh.collect({"queries": ["test"], "limit": 1})

        assert len(items) == 1
        # published_at should be the created_at date, NOT pushed_at.
        assert items[0]["published_at"].startswith("2024-01-01")
        assert not items[0]["published_at"].startswith("2026-07-26")

    @pytest.mark.asyncio
    async def test_last_activity_stored_in_raw_json(self):
        """pushed_at should be stored as _last_activity in raw_json, not as published_at."""
        repo = self._github_repo()
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"items": [repo]}
        fake_resp.raise_for_status = MagicMock()

        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=fake_resp)
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        with patch("newsbot.collectors.github.httpx.AsyncClient", return_value=fake_client):
            items = await gh.collect({"queries": ["test"], "limit": 1})

        raw = items[0].get("raw_json", {})
        assert "_last_activity" in raw
        assert raw["_last_activity"].startswith("2026-07-26")


class TestDocumentationCorrectness:
    """Verify README and env.example match current application."""

    def test_env_example_has_all_required_vars(self):
        """.env.example should include all environment variables used by the app."""
        with open(".env.example") as f:
            content = f.read()
        required_vars = [
            "BOT_TOKEN", "NEWS_CHANNEL_ID", "LM_BASE", "LM_MODEL",
            "LM_API_KEY", "LM_TIMEOUT", "PH_API_KEY", "GITHUB_TOKEN",
            "NEWS_INTERVAL_HOURS", "NEWS_POST_INTERVAL_MINUTES",
            "ADMIN_USER_ID", "NEWS_DB",
        ]
        for var in required_vars:
            assert var in content, f".env.example missing {var}"

    def test_docker_env_example_has_all_required_vars(self):
        """deploy/docker/env.example should include all required vars."""
        with open("deploy/docker/env.example") as f:
            content = f.read()
        required_vars = [
            "BOT_TOKEN", "NEWS_CHANNEL_ID", "LM_BASE", "LM_MODEL",
            "NEWS_INTERVAL_HOURS", "NEWS_POST_INTERVAL_MINUTES",
            "ADMIN_USER_ID",
        ]
        for var in required_vars:
            assert var in content, f"deploy/docker/env.example missing {var}"

    def test_readme_documents_digest_not_run(self):
        """README should document /digest and /post, not obsolete /run."""
        with open("README.md") as f:
            content = f.read()
        assert "/digest" in content, "README should mention /digest command"
        assert "/post" in content, "README should mention /post command"
        # /run should not appear as a command (it was removed)
        assert "| `/run`" not in content, "README should not list /run as a command"

    def test_readme_lm_base_includes_v1(self):
        """README should say LM_BASE includes /v1."""
        with open("README.md") as f:
            content = f.read()
        assert "/v1" in content, "README should mention LM_BASE includes /v1"