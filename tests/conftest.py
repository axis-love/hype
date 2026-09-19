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
