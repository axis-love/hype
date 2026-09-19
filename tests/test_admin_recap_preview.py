"""Admin recap_preview timezone wiring."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from newsbot.admin import AdminActions
from newsbot.env import Env


async def test_recap_preview_passes_news_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str | None] = {}

    def fake_local_now(tz_name: str | None = None) -> datetime:
        seen["tz"] = tz_name
        return datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr("newsbot.admin.local_now", fake_local_now)
    store = MagicMock()
    store.list_posted_since.return_value = []
    env = Env.from_environ({"NEWS_TZ": "Asia/Makassar"})
    admin = AdminActions(store, MagicMock(), MagicMock(), [5], env)
    with pytest.raises(RuntimeError, match="nothing posted"):
        await admin.recap_preview()
    assert seen["tz"] == "Asia/Makassar"
