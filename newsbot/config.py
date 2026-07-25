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
    "reddit": 1.0,
    "github": 1.1,
    "producthunt": 0.8,
    "huggingface_papers": 1.2,
    "lobsters": 1.0,
    "rss": 0.5,           # normal RSS
    "official_rss": 1.3,  # tagged via feed 'weight' override
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
    "max_candidates": 40,
    "max_final_news": 8,
    "min_score": 35,
    "source_quota": 8,
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
    "Include the source link on its own line at the end."
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

    sources = _coerce_dict(raw.get("sources"), DEFAULT_SOURCES)
    # Drop sources the operator disabled (set to None or empty dict).
    sources = {k: v for k, v in sources.items() if v}

    return {
        "sources": sources,
        "source_weights": _coerce_dict(raw.get("source_weights"), DEFAULT_SOURCE_WEIGHTS),
        "topic_boost": {**DEFAULT_TOPIC_BOOST, **_coerce_dict(raw.get("topic_boost"), {})},
        "lookback_hours": _as_int(raw.get("lookback_hours"), DEFAULT_RUN["lookback_hours"]),
        "max_candidates": _as_int(raw.get("max_candidates"), DEFAULT_RUN["max_candidates"]),
        "max_final_news": _as_int(raw.get("max_final_news"), DEFAULT_RUN["max_final_news"]),
        "min_score": _as_float(raw.get("min_score"), DEFAULT_RUN["min_score"]),
        "source_quota": _as_int(raw.get("source_quota"), DEFAULT_RUN["source_quota"]),
        "item_prune_hours": _as_int(raw.get("item_prune_hours"), DEFAULT_RUN["item_prune_hours"]),
        "llm_temperature": _as_float(raw.get("llm_temperature"), DEFAULT_LLM["temperature"]),
        "llm_max_tokens_filter": _as_int(raw.get("llm_max_tokens_filter"), DEFAULT_LLM["max_tokens_filter"]),
        "llm_max_tokens_digest": _as_int(raw.get("llm_max_tokens_digest"), DEFAULT_LLM["max_tokens_digest"]),
        "style_prompt": str(raw.get("style_prompt") or DEFAULT_STYLE_PROMPT),
    }


def _coerce_dict(value: Any, default: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return dict(default)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default