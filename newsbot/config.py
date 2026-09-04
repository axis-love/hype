"""News bot configuration.

Reads the SettingsStore 'news' namespace for source lists, weights,
topic boosts, and runtime parameters. Provides hard-coded defaults
matching the architecture spec §8 so the bot runs
out of the box with an empty settings table — the operator can override
any key via SQLite or the (future) admin path.
"""

from __future__ import annotations

import math
import os
from typing import Any

from core.settings_store import SettingsStore
from newsbot.collectors.base import VALID_SOURCE_KEYS
from newsbot.topics import (
    DEFAULT_TOPIC_PACKS,
    derive_config as _derive_topic_config,
    merge_packs as _merge_topic_packs,
    validate_topic_overrides as _validate_topic_overrides,
)


# --- Defaults (from the concept doc §8) -------------------------------

DEFAULT_SOURCE_WEIGHTS: dict[str, float] = {
    "hackernews": 1.2,
    "hn": 1.2,  # alias — HN collector uses "hn" as source
    "reddit": 1.0,  # H-5 tuning: 0.8 depressed high-engagement Reddit items
    "github": 1.1,
    "huggingface_papers": 1.2,
    "rss": 0.5,           # normal RSS
    "official_rss": 1.3,  # tagged via feed 'weight' override
    "trends": 0.6,        # Google Trends: traffic as reposts signal
}

# Source identifier normalization: maps collector source IDs to weight-map keys.
# This ensures "hn" from the HN collector matches "hackernews" in the weight map.
_SOURCE_ALIASES: dict[str, str] = {
    "hn": "hackernews",
}

# --- Topic packs (derived from DEFAULT_TOPIC_PACKS) -------------------

# Backward-compat: DEFAULT_TOPIC_BOOST and TOPIC_KEYWORDS are derived
# from the topic packs so existing imports keep working. The pack table
# in newsbot/topics.py is the source of truth.
_DERIVED = _derive_topic_config(DEFAULT_TOPIC_PACKS)
DEFAULT_TOPIC_BOOST: dict[str, int] = _DERIVED["topic_boost"]
TOPIC_KEYWORDS: dict[str, list[str]] = _DERIVED["topic_keywords"]

DEFAULT_RUN: dict[str, Any] = {
    "lookback_hours": 48,
    "max_candidates": 20,
    # v2: the store must feed 12 posts/day even when the threshold rejects,
    # so digest carries more survivors per cycle.
    "max_final_news": 14,
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

# Default system prompt for the daily recap (llm_daily_summary). Overridable
# via SQLite setting news.recap_prompt or the /setrecap bot command.
# Contract: STRICT JSON {"title": "..."} — headline only, no per-item summaries.
# The application owns all layout and links (richmd.render_recap).
# NOTE: If Anton has a custom /setrecap override from the OQ-1 era (asking
# for {"title","items":[{"id","summary"}]}), it will override this default
# until reset via /setrecap default. The new llm_daily_summary ignores the
# "items" key in the LLM response — a stale prompt that asks for items won't
# crash, but the extra LLM output is wasted.
DEFAULT_RECAP_PROMPT = (
    "You write the daily recap headline for a Telegram tech-news channel. "
    "You receive the posts published in the last 24 hours. "
    "Return STRICT JSON: {\"title\": \"...\"}. "
    "The title is ONE headline summarizing the whole day (short, no hype words, no emojis). "
    "Do NOT add an 'items' key. Do NOT add any other fields. "
    "The application renders the final layout — return data only, no formatting."
)


# --- Consumer profiles (design note §3) ---------------------------------
#
# Each consumer profile carries its own selection knobs (floor, ratio,
# cooldown_max, max_candidates) and topic filter. The telegram profile
# mirrors today's env defaults so behaviour is unchanged. The girllm
# profile reads from HYPE_CONSUMER_GIRLLM_* env with sane defaults.
#
# load_config returns config["consumers"] as a dict[consumer_name -> profile].

def _consumer_profiles() -> dict[str, dict[str, Any]]:
    """Build consumer profiles from env defaults.

    telegram: mirrors today's env knobs (NEWS_TEMP_FLOOR etc.).
    girllm: reads HYPE_CONSUMER_GIRLLM_* env with sane defaults
            (floor 25, ratio 0.3, cooldown 2, max_candidates 5).
    blog: reads HYPE_CONSUMER_BLOG_* env with sane defaults
          (floor 55, ratio 0.8, cooldown 3, max_candidates 5).
          Topics: science, new_research, ai.
    """
    tg_floor = float(os.getenv("NEWS_TEMP_FLOOR", "35"))
    tg_ratio = float(os.getenv("NEWS_THRESHOLD_RATIO", "0.5"))
    tg_cooldown = int(os.getenv("NEWS_TOPIC_COOLDOWN_MAX", "3"))
    return {
        "telegram": {
            "channel": "telegram",
            "floor": tg_floor,
            "ratio": tg_ratio,
            "merge_bonus": float(os.getenv("NEWS_MERGE_BONUS", "0.2")),
            "merge_cap": float(os.getenv("NEWS_MERGE_CAP", "2.0")),
            "cooldown_max": tg_cooldown,
            "max_candidates": int(os.getenv("NEWS_MAX_CANDIDATES", "20")),
            "topics": None,  # None = no topic filter (all topics)
        },
        "girllm": {
            "channel": "girllm",
            "floor": float(os.getenv("HYPE_CONSUMER_GIRLLM_FLOOR", "25")),
            "ratio": float(os.getenv("HYPE_CONSUMER_GIRLLM_RATIO", "0.3")),
            "merge_bonus": float(os.getenv("NEWS_MERGE_BONUS", "0.2")),
            "merge_cap": float(os.getenv("NEWS_MERGE_CAP", "2.0")),
            "cooldown_max": int(os.getenv("HYPE_CONSUMER_GIRLLM_COOLDOWN_MAX", "2")),
            "max_candidates": int(os.getenv("HYPE_CONSUMER_GIRLLM_MAX_CANDIDATES", "5")),
            "topics": ["gaming", "gamedev", "ai"],
        },
        "blog": {
            "channel": "blog",
            "floor": float(os.getenv("HYPE_CONSUMER_BLOG_FLOOR", "55")),
            "ratio": float(os.getenv("HYPE_CONSUMER_BLOG_RATIO", "0.8")),
            "merge_bonus": float(os.getenv("NEWS_MERGE_BONUS", "0.2")),
            "merge_cap": float(os.getenv("NEWS_MERGE_CAP", "2.0")),
            "cooldown_max": int(os.getenv("HYPE_CONSUMER_BLOG_COOLDOWN_MAX", "3")),
            "max_candidates": int(os.getenv("HYPE_CONSUMER_BLOG_MAX_CANDIDATES", "5")),
            "topics": ["science", "new_research", "ai"],
        },
    }


def consumer_profile(config: dict[str, Any], name: str) -> dict[str, Any]:
    """Look up a consumer profile by name, raising ValueError if unknown.

    The API key -> consumer mapping (H4) and every selection call site
    resolve profiles through this helper so an unknown consumer fails
    loudly with the name in the message instead of a silent KeyError
    or None-deref.

    Raises ValueError: unknown consumer: <name>
    """
    profiles = config.get("consumers") or {}
    profile = profiles.get(name)
    if profile is None:
        raise ValueError(f"unknown consumer: {name}")
    return profile


# --- Default source config (so the bot runs with no settings) ---------

DEFAULT_SOURCES: dict[str, Any] = {
    "hackernews": {"tags": "front_page", "limit": 15},
    "huggingface_papers": {"limit": 10},
    "trends": {
        "geos": ["US"],
        "limit": 3,
    },
}


def load_config(settings: SettingsStore) -> dict[str, Any]:
    """Read the 'news' namespace from SettingsStore and merge with defaults.

    Topic packs (newsbot/topics.py) are the source of truth for which
    subreddits, RSS feeds, GitHub queries, and topic boosts are active.
    The operator can override individual packs via the settings key
    'news.topics' (partial dict merged over defaults, e.g.
    {"gaming": {"enabled": true}, "ai": {"enabled": false}}).

    Explicit 'news.sources' overrides are merged over the pack-derived
    sources — use this for non-topic sources (HN front page) or to
    add/replace specific source blocks.

    Recognized keys (all optional; defaults applied if missing):
      news.topics          — partial dict merged over DEFAULT_TOPIC_PACKS
      news.sources         — dict[source] -> source config (merged over packs)
      news.source_weights   — dict (DEFAULT_SOURCE_WEIGHTS)
      news.topic_boost      — dict (merged over pack-derived boosts)
      news.lookback_hours   — int (48)
      news.max_candidates    — int (80)
      news.max_final_news    — int (14)
      news.min_score         — float (35)
      news.item_prune_hours  — int (48)
      news.llm_temperature   — float (0.4)
      news.llm_max_tokens_filter  — int (800)
      news.llm_max_tokens_digest  — int (1500)
      news.style_prompt           — str (DEFAULT_STYLE_PROMPT)
      news.recap_prompt           — str (DEFAULT_RECAP_PROMPT)

    Returns ``shadowed_sources``: list of source keys whose blocks came
    from an explicit ``news.sources`` override rather than topic packs.
    /sources and /topic surface this so the operator knows a /topic toggle
    is inert for those blocks.
    """
    raw = settings.list("news") if hasattr(settings, "list") else {}

    # --- Topic packs: the source of truth for sources/boosts/keywords ---
    topic_overrides = _coerce_dict(raw.get("topics"), {}, key="topics")
    topic_errors = _validate_topic_overrides(topic_overrides)
    if topic_errors:
        raise ValueError(
            "Configuration validation failed:\n  " + "\n  ".join(topic_errors)
        )
    packs = _merge_topic_packs(topic_overrides)
    derived = _derive_topic_config(packs)

    # Pack-derived sources are the defaults; explicit news.sources overrides
    # are merged ON TOP (per-source-block: if the operator sets
    # news.sources.reddit, that replaces the reddit block entirely).
    explicit_sources = _coerce_dict(raw.get("sources"), {}, key="sources")
    sources = dict(derived["sources"])
    # HN is not topic-specific — keep it as a default if not explicitly set.
    if "hackernews" not in sources:
        sources["hackernews"] = dict(DEFAULT_SOURCES["hackernews"])
    # HuggingFace papers: new_research pack lives here, but the collector
    # needs its own source block.
    if "huggingface_papers" not in sources:
        sources["huggingface_papers"] = dict(DEFAULT_SOURCES["huggingface_papers"])
    # Google Trends is not topic-specific — keep it as a default if not
    # explicitly set. Without this the collector never dispatches in prod
    # (collect_all only runs trends when "trends" is in config["sources"]).
    if "trends" not in sources:
        sources["trends"] = dict(DEFAULT_SOURCES["trends"])
    # Merge explicit overrides over pack-derived sources.
    shadowed_sources: list[str] = []
    for src_key, src_cfg in explicit_sources.items():
        if src_cfg:
            sources[src_key] = src_cfg
            # A non-topic explicit override shadows the pack-derived block
            # (or the default block for HN/HF/Trends). Report it so /sources
            # and /topic can surface the signal to the operator.
            shadowed_sources.append(src_key)
        else:
            sources.pop(src_key, None)
    # Drop sources the operator disabled (set to None or empty dict).
    sources = {k: v for k, v in sources.items() if v}

    # Topic boosts: pack-derived + explicit overrides.
    topic_boost = dict(derived["topic_boost"])
    topic_boost.update(_coerce_dict(raw.get("topic_boost"), {}, key="topic_boost"))

    config = {
        "sources": sources,
        "shadowed_sources": shadowed_sources,
        "source_weights": _coerce_dict(raw.get("source_weights"), DEFAULT_SOURCE_WEIGHTS, key="source_weights"),
        "topic_boost": topic_boost,
        "topic_keywords": derived["topic_keywords"],
        "source_topic_map": derived["source_topic_map"],
        "topic_packs": packs,
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
        "recap_prompt": str(raw.get("recap_prompt") or DEFAULT_RECAP_PROMPT),
        "consumers": _consumer_profiles(),
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

    # min_score range check (0.0-100.0).
    min_score = config.get("min_score", 0.0)
    if not isinstance(min_score, (int, float)):
        errors.append(f"min_score must be numeric, got {type(min_score).__name__}")
    elif not (0.0 <= float(min_score) <= 100.0):
        errors.append(f"min_score must be in [0.0, 100.0], got {min_score}")

    # Source weights validation.
    for src, w in config["source_weights"].items():
        if not isinstance(w, (int, float)):
            errors.append(f"source_weights['{src}'] must be numeric, got {type(w).__name__}")
        elif w <= 0:
            errors.append(f"source_weights['{src}'] must be > 0")

    # topic_boost validation: keys should be known source names or topic keys,
    # values must be numeric and non-negative.
    valid_topic_keys = set(DEFAULT_TOPIC_BOOST.keys()) | set(config.get("source_weights", {}).keys())
    for key, val in (config.get("topic_boost") or {}).items():
        if not isinstance(val, (int, float)):
            errors.append(f"topic_boost['{key}'] must be numeric, got {type(val).__name__}")
        elif val < 0:
            errors.append(f"topic_boost['{key}'] must be >= 0")

    # Consumer profiles validation (design note §3).
    consumers = config.get("consumers")
    if not isinstance(consumers, dict):
        errors.append("consumers must be a dict")
    else:
        for c_name, c_prof in consumers.items():
            if not isinstance(c_prof, dict):
                errors.append(f"consumers['{c_name}'] must be a dict, got {type(c_prof).__name__}")
                continue
            for c_field in ("channel", "floor", "ratio", "merge_bonus", "merge_cap", "cooldown_max", "max_candidates", "topics"):
                if c_field not in c_prof:
                    errors.append(f"consumers['{c_name}'] missing required field {c_field!r}")
            if not isinstance(c_prof.get("channel"), str) or not c_prof.get("channel"):
                errors.append(f"consumers['{c_name}'].channel must be a non-empty string, got {c_prof.get('channel')!r}")
            for c_field in ("floor", "ratio", "merge_bonus", "merge_cap"):
                c_val = c_prof.get(c_field)
                if c_val is not None and not isinstance(c_val, (int, float)):
                    errors.append(f"consumers['{c_name}']['{c_field}'] must be numeric, got {type(c_val).__name__}")
            # cooldown_max may be 0 (disables the filter — env contract);
            # max_candidates must be a positive int.
            c_cd = c_prof.get("cooldown_max")
            if c_cd is not None and (not isinstance(c_cd, int) or c_cd < 0):
                errors.append(f"consumers['{c_name}']['cooldown_max'] must be a non-negative int, got {c_cd!r}")
            c_mc = c_prof.get("max_candidates")
            if c_mc is not None and (not isinstance(c_mc, int) or c_mc <= 0):
                errors.append(f"consumers['{c_name}']['max_candidates'] must be a positive int, got {c_mc!r}")
            c_topics = c_prof.get("topics")
            if c_topics is not None and (not isinstance(c_topics, list) or not all(isinstance(t, str) and t for t in c_topics)):
                errors.append(f"consumers['{c_name}']['topics'] must be None or a list of non-empty strings")

    # Nested source config validation.
    sources = config.get("sources") or {}
    if not isinstance(sources, dict):
        errors.append("sources must be a dict")
    else:
        _VALID_SORT_VALUES = {"stars", "forks", "updated", "best-match", "help-wanted-issues"}

        # Reject unknown source blocks. VALID_SOURCE_KEYS is the single
        # source of truth imported from collectors/base.py — adding a
        # collector there automatically extends config validation.
        for src_key in sources:
            if src_key not in VALID_SOURCE_KEYS:
                errors.append(f"unknown source {src_key!r} — valid: {', '.join(sorted(VALID_SOURCE_KEYS))}")

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
                                errors.append(f"rss.feeds[{i}] must be a dict, got {type(feed).__name__}")
                            elif not feed.get("url"):
                                errors.append(f"rss.feeds[{i}] missing 'url'")
                            elif not feed.get("name"):
                                errors.append(f"rss.feeds[{i}] missing 'name'")
                            else:
                                fw = feed.get("weight")
                                if fw is not None:
                                    if not isinstance(fw, (int, float)):
                                        errors.append(f"rss.feeds[{i}].weight must be numeric, got {type(fw).__name__}")
                                    elif fw < 0:
                                        errors.append(f"rss.feeds[{i}].weight must be non-negative")
                                    elif isinstance(fw, float) and (math.isnan(fw) or math.isinf(fw)):
                                        errors.append(f"rss.feeds[{i}].weight must be finite, got {fw}")

        # HN config validation.
        hn_config = sources.get("hackernews")
        if hn_config is not None:
            if not isinstance(hn_config, dict):
                errors.append("sources.hackernews must be a dict")
            else:
                hn_limit = hn_config.get("limit")
                if hn_limit is not None:
                    if not isinstance(hn_limit, int):
                        errors.append("sources.hackernews.limit must be int")
                    elif hn_limit <= 0 or hn_limit > 100:
                        errors.append("sources.hackernews.limit must be in [1, 100]")
                hn_tags = hn_config.get("tags")
                if hn_tags is not None and not isinstance(hn_tags, str):
                    errors.append("sources.hackernews.tags must be a string")
                hn_queries = hn_config.get("queries")
                if hn_queries is not None:
                    if not isinstance(hn_queries, list):
                        errors.append("sources.hackernews.queries must be a list")
                    else:
                        for i, q in enumerate(hn_queries):
                            if not isinstance(q, str):
                                errors.append(f"sources.hackernews.queries[{i}] must be a string")

        # Reddit config validation.
        reddit_config = sources.get("reddit")
        if reddit_config is not None:
            if not isinstance(reddit_config, dict):
                errors.append("sources.reddit must be a dict")
            else:
                subs = reddit_config.get("subreddits")
                if subs is not None:
                    if not isinstance(subs, list):
                        errors.append("sources.reddit.subreddits must be a list")
                    else:
                        for i, sub in enumerate(subs):
                            if not isinstance(sub, str):
                                errors.append(f"sources.reddit.subreddits[{i}] must be a string, got {type(sub).__name__}")
                rl = reddit_config.get("limit")
                if rl is not None:
                    if not isinstance(rl, int):
                        errors.append("sources.reddit.limit must be int")
                    elif rl <= 0 or rl > 100:
                        errors.append("sources.reddit.limit must be in [1, 100]")

        # GitHub config validation.
        gh_config = sources.get("github")
        if gh_config is not None:
            if not isinstance(gh_config, dict):
                errors.append("sources.github must be a dict")
            else:
                queries = gh_config.get("queries")
                if queries is not None:
                    if not isinstance(queries, list):
                        errors.append("sources.github.queries must be a list")
                    else:
                        for i, q in enumerate(queries):
                            if not isinstance(q, str):
                                errors.append(f"sources.github.queries[{i}] must be a string, got {type(q).__name__}")
                gl = gh_config.get("limit")
                if gl is not None:
                    if not isinstance(gl, int):
                        errors.append("sources.github.limit must be int")
                    elif gl <= 0 or gl > 100:
                        errors.append("sources.github.limit must be in [1, 100]")
                sort_val = gh_config.get("sort")
                if sort_val is not None and sort_val not in _VALID_SORT_VALUES:
                    errors.append(f"sources.github.sort must be one of {_VALID_SORT_VALUES}, got {sort_val!r}")

        # HuggingFace Papers config validation.
        hf_config = sources.get("huggingface_papers")
        if hf_config is not None:
            if not isinstance(hf_config, dict):
                errors.append("sources.huggingface_papers must be a dict")
            else:
                hf_limit = hf_config.get("limit")
                if hf_limit is not None:
                    if not isinstance(hf_limit, int):
                        errors.append("sources.huggingface_papers.limit must be int")
                    elif hf_limit <= 0 or hf_limit > 100:
                        errors.append("sources.huggingface_papers.limit must be in [1, 100]")

        # Trends config validation.
        trends_config = sources.get("trends")
        if trends_config is not None:
            if not isinstance(trends_config, dict):
                errors.append("sources.trends must be a dict")
            else:
                geos = trends_config.get("geos")
                if geos is not None:
                    if not isinstance(geos, list):
                        errors.append("sources.trends.geos must be a list")
                    else:
                        for i, geo in enumerate(geos):
                            if not isinstance(geo, str):
                                errors.append(f"sources.trends.geos[{i}] must be a string")
                tr_limit = trends_config.get("limit")
                if tr_limit is not None:
                    if not isinstance(tr_limit, int):
                        errors.append("sources.trends.limit must be int")
                    elif tr_limit <= 0 or tr_limit > 3:
                        errors.append("sources.trends.limit must be in [1, 3]")

    # Source weights: reject NaN/Infinity.
    for src, w in config["source_weights"].items():
        if not isinstance(w, (int, float)):
            errors.append(f"source_weights['{src}'] must be numeric, got {type(w).__name__}")
        elif w <= 0:
            errors.append(f"source_weights['{src}'] must be > 0")
        elif isinstance(w, float) and (math.isnan(w) or math.isinf(w)):
            errors.append(f"source_weights['{src}'] must be finite, got {w}")

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