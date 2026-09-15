"""Process environment as a frozen object.

Callers receive Env; they do not read the process environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping


def _raw(environ: Mapping[str, str], key: str, default: str) -> str:
    if environ is os.environ:
        val = os.getenv(key)
    else:
        val = environ.get(key)
    if val is None:
        return default
    return str(val)


def _int(environ: Mapping[str, str], key: str, default: int) -> int:
    raw = environ.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from None


def _float(environ: Mapping[str, str], key: str, default: float) -> float:
    raw = environ.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        raise ValueError(f"{key} must be numeric, got {raw!r}") from None


def parse_api_keys(raw: str | None) -> dict[str, str]:
    """Parse ``HYPE_API_KEYS`` ``\"consumer:token,...\"`` into ``{token: consumer}``."""
    if not raw:
        return {}
    result: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        consumer, _, token = entry.partition(":")
        consumer = consumer.strip()
        token = token.strip()
        if consumer and token:
            result[token] = consumer
    return result


def _consumers(
    *,
    temp_floor: float,
    threshold_ratio: float,
    merge_bonus: float,
    merge_cap: float,
    topic_cooldown_max: int,
    max_candidates: int,
    girllm_floor: float,
    girllm_ratio: float,
    girllm_cooldown: int,
    girllm_max: int,
    blog_floor: float,
    blog_ratio: float,
    blog_cooldown: int,
    blog_max: int,
) -> dict[str, dict[str, Any]]:
    return {
        "telegram": {
            "channel": "telegram",
            "floor": temp_floor,
            "ratio": threshold_ratio,
            "merge_bonus": merge_bonus,
            "merge_cap": merge_cap,
            "cooldown_max": topic_cooldown_max,
            "max_candidates": max_candidates,
            "topics": None,
        },
        "girllm": {
            "channel": "girllm",
            "floor": girllm_floor,
            "ratio": girllm_ratio,
            "merge_bonus": merge_bonus,
            "merge_cap": merge_cap,
            "cooldown_max": girllm_cooldown,
            "max_candidates": girllm_max,
            "topics": ["gaming", "gamedev", "ai"],
        },
        "blog": {
            "channel": "blog",
            "floor": blog_floor,
            "ratio": blog_ratio,
            "merge_bonus": merge_bonus,
            "merge_cap": merge_cap,
            "cooldown_max": blog_cooldown,
            "max_candidates": blog_max,
            "topics": ["science", "new_research", "ai"],
        },
    }


@dataclass(frozen=True, slots=True)
class Env:
    bot_token: str = ""
    news_channel_id: str = ""
    admin_user_id: str = ""
    lm_base: str = ""
    lm_model: str = ""
    lm_filter_model: str = ""
    lm_api_key: str = ""
    lm_timeout: float = 300.0
    news_db: str = "data/newsbot.sqlite"
    news_tz: str = "Asia/Bangkok"
    gen_hours: str = "5,9,13,17,21"
    store_cap: int = 36
    temp_floor: float = 35.0
    threshold_ratio: float = 0.5
    merge_bonus: float = 0.2
    merge_cap: float = 2.0
    topic_cooldown_max: int = 3
    merge_window_days: int = 7
    retention_posted_days: int = 30
    retention_seen_days: int = 14
    api_port: int | None = None
    api_keys: dict[str, str] = field(default_factory=dict)
    consumers: dict[str, dict[str, Any]] = field(default_factory=dict)
    github_token: str = ""
    max_candidates: int = 20

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> Env:
        env = os.environ if environ is None else environ
        api_port_raw = _raw(env, "HYPE_API_PORT", "").strip()
        if api_port_raw == "":
            api_port: int | None = None
        else:
            try:
                api_port = int(api_port_raw)
            except ValueError:
                raise ValueError(
                    f"HYPE_API_PORT must be an integer, got {api_port_raw!r}"
                ) from None

        temp_floor = _float(env, "NEWS_TEMP_FLOOR", 35.0)
        threshold_ratio = _float(env, "NEWS_THRESHOLD_RATIO", 0.5)
        merge_bonus = _float(env, "NEWS_MERGE_BONUS", 0.2)
        merge_cap = _float(env, "NEWS_MERGE_CAP", 2.0)
        topic_cooldown_max = _int(env, "NEWS_TOPIC_COOLDOWN_MAX", 3)
        max_candidates = _int(env, "NEWS_MAX_CANDIDATES", 20)
        consumers = _consumers(
            temp_floor=temp_floor,
            threshold_ratio=threshold_ratio,
            merge_bonus=merge_bonus,
            merge_cap=merge_cap,
            topic_cooldown_max=topic_cooldown_max,
            max_candidates=max_candidates,
            girllm_floor=_float(env, "HYPE_CONSUMER_GIRLLM_FLOOR", 25.0),
            girllm_ratio=_float(env, "HYPE_CONSUMER_GIRLLM_RATIO", 0.3),
            girllm_cooldown=_int(env, "HYPE_CONSUMER_GIRLLM_COOLDOWN_MAX", 2),
            girllm_max=_int(env, "HYPE_CONSUMER_GIRLLM_MAX_CANDIDATES", 5),
            blog_floor=_float(env, "HYPE_CONSUMER_BLOG_FLOOR", 55.0),
            blog_ratio=_float(env, "HYPE_CONSUMER_BLOG_RATIO", 0.8),
            blog_cooldown=_int(env, "HYPE_CONSUMER_BLOG_COOLDOWN_MAX", 3),
            blog_max=_int(env, "HYPE_CONSUMER_BLOG_MAX_CANDIDATES", 5),
        )
        return cls(
            bot_token=_raw(env, "BOT_TOKEN", "").strip(),
            news_channel_id=_raw(env, "NEWS_CHANNEL_ID", "").strip(),
            admin_user_id=_raw(env, "ADMIN_USER_ID", "").strip(),
            lm_base=_raw(env, "LM_BASE", "").rstrip("/"),
            lm_model=_raw(env, "LM_MODEL", "").strip(),
            lm_filter_model=_raw(env, "LM_FILTER_MODEL", "").strip(),
            lm_api_key=_raw(env, "LM_API_KEY", "").strip(),
            lm_timeout=_float(env, "LM_TIMEOUT", 300.0),
            news_db=_raw(env, "NEWS_DB", "data/newsbot.sqlite"),
            news_tz=_raw(env, "NEWS_TZ", "Asia/Bangkok") or "Asia/Bangkok",
            gen_hours=_raw(env, "NEWS_GEN_HOURS", "5,9,13,17,21"),
            store_cap=_int(env, "NEWS_STORE_CAP", 36),
            temp_floor=temp_floor,
            threshold_ratio=threshold_ratio,
            merge_bonus=merge_bonus,
            merge_cap=merge_cap,
            topic_cooldown_max=topic_cooldown_max,
            merge_window_days=_int(env, "NEWS_MERGE_WINDOW_DAYS", 7),
            retention_posted_days=_int(env, "NEWS_RETENTION_POSTED_DAYS", 30),
            retention_seen_days=_int(env, "NEWS_RETENTION_SEEN_DAYS", 14),
            api_port=api_port,
            api_keys=parse_api_keys(_raw(env, "HYPE_API_KEYS", "").strip()),
            consumers=consumers,
            github_token=_raw(env, "GITHUB_TOKEN", "").strip(),
            max_candidates=max_candidates,
        )


def resolve(env: Env | None) -> Env:
    return env if env is not None else Env.from_environ()
