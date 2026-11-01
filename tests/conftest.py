"""Pytest config for the news bot tests."""

from collections.abc import Iterator
import sys
from pathlib import Path

import pytest

# Ensure the repo root is importable so `from newsbot...` / `from core...` work.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _no_article_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generation reads each candidate's article page; tests never hit the
    network. Candidates keep their collector raw_text as source text.
    tests/test_article.py exercises the fetch path with its own client."""
    import newsbot.article as article

    async def _no_fetch(url, client):
        return ""

    monkeypatch.setattr(article, "fetch_article_text", _no_fetch)


@pytest.fixture(autouse=True)
def _clear_load_config_cache() -> Iterator[None]:
    """Drop the module-level load_config cache between tests.

    The production cache lives on the SettingsStore instance; this clears
    any leftover module global so tests that monkeypatch NEWS_* env and
    build a fresh store cannot pick up a previous run's config.
    """
    import newsbot.config as news_config

    news_config._config_cache = None
    yield
    news_config._config_cache = None
