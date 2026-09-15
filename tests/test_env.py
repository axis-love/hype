"""H13 Env.from_environ parsing."""
from __future__ import annotations

import pytest

from newsbot.env import Env


def test_from_environ_defaults():
    env = Env.from_environ({})
    assert env.news_tz == "Asia/Bangkok"
    assert env.store_cap == 36
    assert env.temp_floor == 35.0
    assert env.threshold_ratio == 0.5
    assert env.merge_bonus == 0.2
    assert env.merge_cap == 2.0
    assert env.topic_cooldown_max == 3
    assert env.merge_window_days == 7
    assert env.retention_posted_days == 30
    assert env.retention_seen_days == 14
    assert env.lm_timeout == 300.0
    assert env.api_port is None
    assert env.api_keys == {}
    assert env.consumers["telegram"]["floor"] == 35.0
    assert env.consumers["girllm"]["floor"] == 25.0
    assert env.consumers["blog"]["floor"] == 55.0


def test_from_environ_rejects_bad_int():
    with pytest.raises(ValueError, match="NEWS_STORE_CAP must be an integer"):
        Env.from_environ({"NEWS_STORE_CAP": "nope"})


def test_from_environ_parses_api_keys():
    env = Env.from_environ({"HYPE_API_KEYS": "a:x,b:y"})
    assert env.api_keys == {"x": "a", "y": "b"}
