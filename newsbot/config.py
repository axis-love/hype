"""News bot configuration.

Reads the SettingsStore 'news' namespace for source lists, weights,
topic boosts, and runtime parameters. Provides hard-coded defaults
matching the architecture spec §8 so the bot runs
out of the box with an empty settings table — the operator can override
any key via SQLite or the (future) admin path.
"""

from __future__ import annotations

from typing import Any

from core.settings_store import SettingsStore


# --- Defaults (from the concept doc §8) -------------------------------

DEFAULT_SOURCE_WEIGHTS: dict[str, float] = {
    "hackernews": 1.2,
    "hn": 1.2,  # alias — HN collector uses "hn" as source
    "reddit": 1.0,
    "github": 1.1,
    "producthunt": 0.8,
    "huggingface_papers": 1.2,
    "lobsters": 1.0,
    "rss": 0.5,           # normal RSS
    "official_rss": 1.3,  # tagged via feed 'weight' override
}

# Source identifier normalization: maps collector source IDs to weight-map keys.
# This ensures "hn" from the HN collector matches "hackernews" in the weight map.
_SOURCE_ALIASES: dict[str, str] = {
    "hn": "hackernews",
}

DEFAULT_TOPIC_BOOST: dict[str, int] = {
    "ai": 20,
    "llm": 20,
    "local_llm": 25,
    "coding_agents": 25,
    "gamedev": 15,
    "unity": 12,
    "unreal": 12,
    "godot": 12,
    "vr_ar": 18,
    "robotics": 18,
    "github_trending": 15,
    "new_research": 20,
}

# Topic keyword → boost-key mapping. Title/snippet containing any keyword
# triggers the boost.
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "ai": ["ai", "artificial intelligence", "machine learning", "ml", "gpt", "claude", "gemini", "llama"],
    "llm": ["llm", "language model", "transformer", "inference", "quantiz", "fine-tune", "fine tune"],
    "local_llm": ["local llm", "ollama", "llama.cpp", "lm studio", "gguf", "exllama", "vllm"],
    "coding_agents": ["coding agent", "code agent", "dev agent", "copilot", "cursor", "aider", "agentic"],
    "gamedev": ["game dev", "gamedev", "game engine", "unity3d", "unreal engine", "godot"],
    "unity": ["unity", "unity3d", "unityengine"],
    "unreal": ["unreal", "ue5", "unrealengine"],
    "godot": ["godot", "gdscript"],
    "vr_ar": ["vr", "ar", "xr", "vr/ar", "virtual reality", "augmented reality", "metaverse", "webxr", "vision pro"],
    "robotics": ["robotics", "robot", "humanoid", "actuator", "ros2", "ros 2", "drone"],
    "github_trending": ["trending", "stars", "github"],
    "new_research": ["research", "paper", "arxiv", "breakthrough", "benchmark", "study"],
}

DEFAULT_RUN: dict[str, Any] = {
    "lookback_hours": 48,
    "max_candidates": 20,
    "max_final_news": 8,
    "min_score": 35,
    "source_quota": 4,
    "item_prune_hours": 48,
}

DEFAULT_LLM: dict[str, Any] = {
    "temperature": 0.4,
    "max_tokens_filter": 8000,
    "max_tokens_digest": 8000,
}

# Default style prompt for Pass B (the styler). Overridable via SQLite
# setting news.style_prompt or the /setstyle bot command.
DEFAULT_STYLE_PROMPT = (
    "You are a tech-news writer for a Telegram channel called Cyber Cream. "
    "Write punchy, concise posts about trending tech news. One post per item. "
    "Each post is 2-4 sentences — hook the reader, explain what happened, and why it matters. "
    "Be direct and conversational, like a sharp friend sharing a find. "
    "No hype words like 'revolutionary' or 'game-changing'. No emojis. No clickbait. "
)

# --- Default source config (so the bot runs with no settings) ---------

DEFAULT_SOURCES: dict[str, Any] = {
    "hackernews": {"tags": "front_page", "limit": 15},
    "reddit": {
        "subreddits": [
            "LocalLLaMA", "MachineLearning", "artificial", "singularity",
            "programming", "gamedev", "Unity3D", "unrealengine", "Godot",
            "virtualreality", "OculusQuest", "selfhosted", "opensource",
        ],
        "limit": 10,
    },
    "github": {
        "queries": ["llm", "agent", "coding-agent", "rag", "local-llm",
                    "unity", "game-engine", "unreal", "godot", "webxr", "vr", "robotics"],
        "limit": 5,
        "sort": "stars",
    },
    "huggingface_papers": {"limit": 10},
    "rss": {
        "feeds": [
            {"name": "OpenAI", "url": "https://openai.com/news/rss.xml", "weight": 1.3},
            {"name": "Google DeepMind", "url": "https://deepmind.google/discover/blog/rss.xml", "weight": 1.3},
            {"name": "Unity", "url": "https://blog.unity.com/feed", "weight": 1.1},
            {"name": "Unreal Engine", "url": "https://www.unrealengine.com/en-US/feed", "weight": 1.1},
        ],
    },
}


def load_config(settings: SettingsStore) -> dict[str, Any]:
    """Read the 'news' namespace from SettingsStore and merge with defaults.

    Recognized keys (all optional; defaults applied if missing):
      news.sources          — dict[source] -> source config (DEFAULT_SOURCES)
      news.source_weights   — dict (DEFAULT_SOURCE_WEIGHTS)
      news.topic_boost      — dict (DEFAULT_TOPIC_BOOST)
      news.lookback_hours   — int (48)
      news.max_candidates    — int (80)
      news.max_final_news    — int (10)
      news.min_score         — float (35)
      news.item_prune_hours  — int (48)
      news.llm_temperature   — float (0.4)
      news.llm_max_tokens_filter  — int (800)
      news.llm_max_tokens_digest  — int (1500)
    """
    raw = settings.list("news") if hasattr(settings, "list") else {}

    sources = _coerce_dict(raw.get("sources"), DEFAULT_SOURCES, key="sources")
    # Drop sources the operator disabled (set to None or empty dict).
    sources = {k: v for k, v in sources.items() if v}

    config = {
        "sources": sources,
        "source_weights": _coerce_dict(raw.get("source_weights"), DEFAULT_SOURCE_WEIGHTS, key="source_weights"),
        "topic_boost": {**DEFAULT_TOPIC_BOOST, **_coerce_dict(raw.get("topic_boost"), {}, key="topic_boost")},
        "lookback_hours": _as_int(raw.get("lookback_hours"), DEFAULT_RUN["lookback_hours"], key="lookback_hours"),
        "max_candidates": _as_int(raw.get("max_candidates"), DEFAULT_RUN["max_candidates"], key="max_candidates"),
        "max_final_news": _as_int(raw.get("max_final_news"), DEFAULT_RUN["max_final_news"], key="max_final_news"),
        "min_score": _as_float(raw.get("min_score"), DEFAULT_RUN["min_score"], key="min_score"),
        "source_quota": _as_int(raw.get("source_quota"), DEFAULT_RUN["source_quota"], key="source_quota"),
        "item_prune_hours": _as_int(raw.get("item_prune_hours"), DEFAULT_RUN["item_prune_hours"], key="item_prune_hours"),
        "llm_temperature": _as_float(raw.get("llm_temperature"), DEFAULT_LLM["temperature"], key="llm_temperature"),
        "llm_max_tokens_filter": _as_int(raw.get("llm_max_tokens_filter"), DEFAULT_LLM["max_tokens_filter"], key="llm_max_tokens_filter"),
        "llm_max_tokens_digest": _as_int(raw.get("llm_max_tokens_digest"), DEFAULT_LLM["max_tokens_digest"], key="llm_max_tokens_digest"),
        "style_prompt": str(raw.get("style_prompt") or DEFAULT_STYLE_PROMPT),
    }

    _validate_config(config)
    return config


def _validate_config(config: dict[str, Any]) -> None:
    """Validate configuration ranges and shapes. Raises ValueError on invalid config."""
    errors: list[str] = []

    # Numeric range checks.
    if not isinstance(config["lookback_hours"], (int, float)):
        errors.append(f"lookback_hours must be numeric, got {type(config['lookback_hours']).__name__}")
    elif config["lookback_hours"] <= 0:
        errors.append("lookback_hours must be > 0")
    if not isinstance(config["max_candidates"], int):
        errors.append(f"max_candidates must be int, got {type(config['max_candidates']).__name__}")
    elif config["max_candidates"] <= 0:
        errors.append("max_candidates must be > 0")
    elif config["max_candidates"] > 100:
        errors.append("max_candidates should be <= 100 (got %s)" % config["max_candidates"])
    if not isinstance(config["max_final_news"], int):
        errors.append(f"max_final_news must be int, got {type(config['max_final_news']).__name__}")
    elif config["max_final_news"] <= 0:
        errors.append("max_final_news must be > 0")
    elif isinstance(config["max_candidates"], int) and config["max_final_news"] > config["max_candidates"]:
        errors.append("max_final_news (%s) cannot exceed max_candidates (%s)" % (config["max_final_news"], config["max_candidates"]))
    if not isinstance(config["source_quota"], int):
        errors.append(f"source_quota must be int, got {type(config['source_quota']).__name__}")
    elif config["source_quota"] < 0:
        errors.append("source_quota must be >= 0")
    if not isinstance(config["item_prune_hours"], (int, float)):
        errors.append(f"item_prune_hours must be numeric, got {type(config['item_prune_hours']).__name__}")
    elif config["item_prune_hours"] <= 0:
        errors.append("item_prune_hours must be > 0")
    if not isinstance(config["llm_temperature"], (int, float)):
        errors.append(f"llm_temperature must be numeric, got {type(config['llm_temperature']).__name__}")
    elif not (0.0 <= config["llm_temperature"] <= 2.0):
        errors.append("llm_temperature must be in [0.0, 2.0]")
    if not isinstance(config["llm_max_tokens_filter"], int):
        errors.append(f"llm_max_tokens_filter must be int, got {type(config['llm_max_tokens_filter']).__name__}")
    elif config["llm_max_tokens_filter"] <= 0:
        errors.append("llm_max_tokens_filter must be > 0")
    if not isinstance(config["llm_max_tokens_digest"], int):
        errors.append(f"llm_max_tokens_digest must be int, got {type(config['llm_max_tokens_digest']).__name__}")
    elif config["llm_max_tokens_digest"] <= 0:
        errors.append("llm_max_tokens_digest must be > 0")

    # Source weights validation.
    for src, w in config["source_weights"].items():
        if not isinstance(w, (int, float)):
            errors.append(f"source_weights['{src}'] must be numeric, got {type(w).__name__}")
        elif w <= 0:
            errors.append(f"source_weights['{src}'] must be > 0")

    # Nested source config validation.
    sources = config.get("sources") or {}
    if not isinstance(sources, dict):
        errors.append("sources must be a dict")
    else:
        # RSS feeds validation (expanded).
        rss_config = sources.get("rss")
        if rss_config is not None:
            if not isinstance(rss_config, dict):
                errors.append("sources.rss must be a dict")
            else:
                feeds = rss_config.get("feeds")
                if feeds is not None:
                    if not isinstance(feeds, list):
                        errors.append("sources.rss.feeds must be a list")
                    else:
                        for i, feed in enumerate(feeds):
                            if not isinstance(feed, dict):
                                errors.append(f"rss.feeds[{i}] must be a dict")
                            elif not feed.get("url"):
                                errors.append(f"rss.feeds[{i}] missing 'url'")
                            elif not feed.get("name"):
                                errors.append(f"rss.feeds[{i}] missing 'name'")
                            else:
                                fw = feed.get("weight")
                                if fw is not None and not isinstance(fw, (int, float)):
                                    errors.append(f"rss.feeds[{i}].weight must be numeric, got {type(fw).__name__}")

        # HN config validation.
        hn_config = sources.get("hackernews")
        if hn_config is not None and not isinstance(hn_config, dict):
            errors.append("sources.hackernews must be a dict")

        # Reddit config validation.
        reddit_config = sources.get("reddit")
        if reddit_config is not None:
            if not isinstance(reddit_config, dict):
                errors.append("sources.reddit must be a dict")
            else:
                subs = reddit_config.get("subreddits")
                if subs is not None and not isinstance(subs, list):
                    errors.append("sources.reddit.subreddits must be a list")
                rl = reddit_config.get("limit")
                if rl is not None and not isinstance(rl, int):
                    errors.append("sources.reddit.limit must be int")

        # GitHub config validation.
        gh_config = sources.get("github")
        if gh_config is not None:
            if not isinstance(gh_config, dict):
                errors.append("sources.github must be a dict")
            else:
                queries = gh_config.get("queries")
                if queries is not None and not isinstance(queries, list):
                    errors.append("sources.github.queries must be a list")
                gl = gh_config.get("limit")
                if gl is not None and not isinstance(gl, int):
                    errors.append("sources.github.limit must be int")

    if errors:
        raise ValueError("Configuration validation failed:\n  " + "\n  ".join(errors))


def _coerce_dict(value: Any, default: dict[str, Any], *, key: str = "") -> dict[str, Any]:
    """Coerce to dict. If value is present but not a dict, raise ValueError."""
    if value is None:
        return dict(default)
    if isinstance(value, dict):
        return value
    raise ValueError(
        f"{key or 'value'} must be a dict, got {type(value).__name__}: {value!r}"
    )


def _as_int(value: Any, default: int, *, key: str = "") -> int:
    """Convert to int. If value is present but not parseable, raise ValueError."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(
            f"{key or 'value'} must be int, got bool: {value!r}"
        )
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != int(value):
            raise ValueError(
                f"{key or 'value'} must be int, got non-integer float: {value!r}"
            )
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{key or 'value'} must be int, got {type(value).__name__}: {value!r}"
        )


def _as_float(value: Any, default: float, *, key: str = "") -> float:
    """Convert to float. If value is present but not parseable, raise ValueError."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(
            f"{key or 'value'} must be numeric, got bool: {value!r}"
        )
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{key or 'value'} must be numeric, got {type(value).__name__}: {value!r}"
        )