"""H13 load_config identity cache."""
from __future__ import annotations

from pathlib import Path

from core.settings_store import SettingsStore, SettingsStoreConfig
from newsbot.config import load_config
from newsbot.env import Env


def test_load_config_returns_same_object_until_set(tmp_path: Path):
    settings = SettingsStore(SettingsStoreConfig(db_path=tmp_path / "s.sqlite"))
    env = Env.from_environ({})
    first = load_config(settings, env)
    second = load_config(settings, env)
    assert first is second

    settings.set("news", "lookback_hours", 12)
    third = load_config(settings, env)
    assert third is not first
    assert third["lookback_hours"] == 12
    settings.close()
