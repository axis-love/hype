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


def test_load_config_cache_distinguishes_env(tmp_path: Path):
    settings = SettingsStore(SettingsStoreConfig(db_path=tmp_path / "s.sqlite"))
    env_a = Env.from_environ({"GITHUB_TOKEN": "tok-a"})
    env_b = Env.from_environ({"GITHUB_TOKEN": "tok-b"})
    cfg_a = load_config(settings, env_a)
    cfg_b = load_config(settings, env_b)
    assert cfg_a is not cfg_b
    assert cfg_a["sources"]["github"].get("token") == "tok-a"
    assert cfg_b["sources"]["github"].get("token") == "tok-b"
    settings.close()


def test_load_config_cache_not_shared_across_stores(tmp_path: Path):
    env = Env.from_environ({})
    first = SettingsStore(SettingsStoreConfig(db_path=tmp_path / "a.sqlite"))
    first.set("news", "lookback_hours", 12)
    assert load_config(first, env)["lookback_hours"] == 12
    first.close()

    second = SettingsStore(SettingsStoreConfig(db_path=tmp_path / "b.sqlite"))
    assert load_config(second, env)["lookback_hours"] == 48
    second.close()
