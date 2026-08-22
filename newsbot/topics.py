"""Topic packs — the configuration unit the app is configured by.

Each pack is a topic (science, gaming, ai, ...) that can be switched on
or off. load_config() derives the flat source blocks (reddit.subreddits,
rss.feeds, github.queries) and the topic_boost/keyword table from
ENABLED packs only. Collectors never see topics (SRP): they keep
receiving the flat sources.* blocks.

Runtime override lives in the settings table as news.topics →
{"gaming": {"enabled": true}, "ai": {"enabled": false}, ...} (partial;
merged over defaults).

Sources that are not topic-specific (HN front page) stay as they are.
"""

from __future__ import annotations

import re
from typing import Any


DEFAULT_TOPIC_PACKS: dict[str, dict[str, Any]] = {
    "ai": {
        "enabled": True,
        "boost": 20,
        "keywords": [
            "ai", "artificial intelligence", "machine learning", "ml",
            "gpt", "claude", "gemini", "llama",
            "llm", "language model", "transformer", "inference",
            "quantize", "quantized", "quantization", "fine-tune", "fine tune",
            "local llm", "ollama", "llama.cpp", "lm studio", "gguf", "exllama", "vllm",
            "coding agent", "code agent", "dev agent", "copilot", "cursor", "aider", "agentic",
        ],
        "subreddits": [
            "LocalLLaMA", "MachineLearning", "artificial", "singularity",
        ],
        "feeds": [
            {"name": "OpenAI", "url": "https://openai.com/news/rss.xml", "weight": 1.3},
            {"name": "Google DeepMind", "url": "https://deepmind.google/discover/blog/rss.xml", "weight": 1.3},
        ],
        "github_queries": ["llm", "agent", "coding-agent", "rag", "local-llm"],
        "source_names": [],
    },
    "gaming": {
        "enabled": True,
        "boost": 20,
        "keywords": [
            "gta", "playstation", "xbox", "nintendo", "steam", "leak",
            "trailer", "gameplay", "rockstar", "valve", "epic games",
        ],
        "subreddits": [
            "gaming", "Games", "GamingLeaksAndRumours",
        ],
        "feeds": [
            {"name": "IGN", "url": "https://feeds.ign.com/ign/all", "weight": 1.1},
            {"name": "Eurogamer", "url": "https://www.eurogamer.net/feed", "weight": 1.1},
        ],
        "github_queries": [],
        "source_names": [],
    },
    "gamedev": {
        "enabled": True,
        "boost": 15,
        "keywords": [
            "game dev", "gamedev", "game engine", "unity3d",
            "unity", "unityengine", "unreal", "ue5", "unrealengine",
            "godot", "gdscript",
        ],
        "subreddits": [
            "gamedev", "Unity3D", "unrealengine", "Godot",
        ],
        "feeds": [
            {"name": "Unity", "url": "https://blog.unity.com/feed", "weight": 1.1},
            {"name": "Unreal Engine", "url": "https://www.unrealengine.com/en-US/feed", "weight": 1.1},
        ],
        "github_queries": ["unity", "game-engine", "unreal", "godot"],
        "source_names": [],
    },
    "science": {
        "enabled": True,
        "boost": 18,
        "keywords": [
            "science", "physics", "biology", "chemistry", "astronomy",
            "neuroscience", "genetics", "climate", "quantum",
        ],
        "subreddits": [
            "science", "askscience",
        ],
        "feeds": [],
        "github_queries": [],
        "source_names": [],
    },
    "hardware": {
        "enabled": True,
        "boost": 15,
        "keywords": [
            "cpu", "gpu", "ram", "ssd", "motherboard", "overclock",
            "nvidia", "amd", "intel", "raspberry pi", "arduino",
        ],
        "subreddits": [
            "hardware", "buildapc",
        ],
        "feeds": [],
        "github_queries": [],
        "source_names": [],
    },
    "vr_ar": {
        "enabled": True,
        "boost": 18,
        "keywords": [
            "vr", "ar", "xr", "vr/ar", "virtual reality",
            "augmented reality", "metaverse", "webxr", "vision pro",
        ],
        "subreddits": [
            "virtualreality", "OculusQuest",
        ],
        "feeds": [],
        "github_queries": ["webxr", "vr"],
        "source_names": [],
    },
    "robotics": {
        "enabled": True,
        "boost": 18,
        "keywords": [
            "robotics", "robot", "humanoid", "actuator",
            "ros2", "ros 2", "drone",
        ],
        "subreddits": [
            "robotics",
        ],
        "feeds": [],
        "github_queries": ["robotics"],
        "source_names": [],
    },
    "new_research": {
        "enabled": True,
        "boost": 20,
        "keywords": [
            # Moved from the ai pack (H-2 review): generic research words
            # mislabelled science stories as ai ("Study finds GPU leak" →
            # ai). They belong with the research pack, whose source is
            # Hugging Face Papers.
            "research", "paper", "arxiv", "breakthrough", "benchmark", "study",
        ],
        "subreddits": [],
        "feeds": [],
        "github_queries": [],
        # Non-topic collectors owned by this pack: their source_name maps
        # here in source_topic_map so origin_topic fires without keywords.
        "source_names": ["Hugging Face Papers"],
    },
    "design": {
        "enabled": False,
        "boost": 0,
        "keywords": [],
        "subreddits": [],
        "feeds": [],
        "github_queries": [],
        "source_names": [],
    },
    "art": {
        "enabled": False,
        "boost": 0,
        "keywords": [],
        "subreddits": [],
        "feeds": [],
        "github_queries": [],
        "source_names": [],
    },
}


def merge_packs(
    overrides: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Merge runtime overrides (news.topics) over DEFAULT_TOPIC_PACKS.

    Each override is a partial dict: keys not present keep their defaults.
    Only the specified keys in each pack are overridden.

    Returns a deep copy — the caller can mutate freely.
    """
    if not overrides:
        return {name: dict(pack) for name, pack in DEFAULT_TOPIC_PACKS.items()}

    merged: dict[str, dict[str, Any]] = {}
    for name, default_pack in DEFAULT_TOPIC_PACKS.items():
        pack = dict(default_pack)
        override = overrides.get(name)
        if isinstance(override, dict):
            for k, v in override.items():
                # Only override known pack keys.
                if k in pack:
                    pack[k] = v
        merged[name] = pack
    return merged


def derive_config(
    packs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Derive flat source blocks, topic_boost, and keyword table from packs.

    Only ENABLED packs contribute their sources, feeds, queries, and boost.
    Disabled packs produce no sources but remain in the pack table (so
    origin_topic lookup can still find them if needed).

    Returns:
        sources: {
            reddit: {subreddits: [...], limit: 10},
            rss: {feeds: [...]},
            github: {queries: [...], limit: 5, sort: "stars"},
        }
        topic_boost: {topic_name: boost, ...} for enabled packs only
        topic_keywords: {topic_name: [keywords]} for enabled packs only
        source_topic_map: {"r/sub": "gaming", "IGN": "gaming", ...}
    """
    subreddits: list[str] = []
    feeds: list[dict[str, Any]] = []
    github_queries: list[str] = []
    topic_boost: dict[str, int] = {}
    topic_keywords: dict[str, list[str]] = {}
    source_topic_map: dict[str, str] = {}

    for name, pack in packs.items():
        if not pack.get("enabled"):
            continue
        boost = int(pack.get("boost") or 0)
        if boost > 0:
            topic_boost[name] = boost
        kws = pack.get("keywords") or []
        if kws:
            topic_keywords[name] = list(kws)
        for sub in pack.get("subreddits") or []:
            sub = str(sub).strip().strip("/")
            if sub and sub not in subreddits:
                subreddits.append(sub)
                source_topic_map[f"r/{sub}"] = name
        for feed in pack.get("feeds") or []:
            if isinstance(feed, dict) and feed.get("url") and feed.get("name"):
                feeds.append(dict(feed))
                source_topic_map[str(feed["name"])] = name
        for q in pack.get("github_queries") or []:
            q = str(q).strip()
            if q and q not in github_queries:
                github_queries.append(q)
        for sname in pack.get("source_names") or []:
            # Non-topic collectors owned by this pack (e.g. "Hugging Face
            # Papers" → new_research): map their source_name so
            # origin_topic fires even with zero keyword hits.
            sname = str(sname).strip()
            if sname:
                source_topic_map[sname] = name

    sources: dict[str, Any] = {
        "reddit": {"subreddits": subreddits, "limit": 10},
        "rss": {"feeds": feeds},
        "github": {"queries": github_queries, "limit": 5, "sort": "stars"},
    }

    return {
        "sources": sources,
        "topic_boost": topic_boost,
        "topic_keywords": topic_keywords,
        "source_topic_map": source_topic_map,
    }


def validate_topic_overrides(
    overrides: dict[str, Any] | None,
) -> list[str]:
    """Validate runtime topic overrides. Returns list of error messages.

    Unknown topic names are rejected — the operator can only override
    packs that exist in DEFAULT_TOPIC_PACKS.
    """
    if not overrides:
        return []
    errors: list[str] = []
    known = set(DEFAULT_TOPIC_PACKS.keys())
    for key in overrides:
        if key not in known:
            errors.append(
                f"unknown topic pack '{key}' — valid: {', '.join(sorted(known))}"
            )
        else:
            val = overrides[key]
            if not isinstance(val, dict):
                errors.append(f"topics['{key}'] must be a dict, got {type(val).__name__}")
            else:
                for k in val:
                    if k not in DEFAULT_TOPIC_PACKS[key]:
                        errors.append(
                            f"topics['{key}'].{k} is not a valid pack field — "
                            f"valid: {', '.join(sorted(DEFAULT_TOPIC_PACKS[key].keys()))}"
                        )
    return errors
