"""Tests for topic packs — H-2.

Verifies:
- Enabling/disabling a pack adds/removes its subs, feeds, queries, boost
  from the derived config.
- Unknown topic names rejected by validation.
- Disabled design/art produce no sources.
- Origin-topic bonus applies without keywords.
- Bonus cap = max, not sum.
- Old stacked AI bonus is gone.
"""

import pytest
from typing import Any

from newsbot.topics import (
    DEFAULT_TOPIC_PACKS,
    derive_config,
    merge_packs,
    validate_topic_overrides,
)
from newsbot.config import DEFAULT_TOPIC_BOOST, TOPIC_KEYWORDS, DEFAULT_SOURCE_WEIGHTS
from newsbot.scoring import score_breakdown, topic_bonus


# --- Pack derivation tests -----------------------------------------------

class TestDeriveConfig:
    """Verify derive_config produces correct flat blocks from packs."""

    def test_all_enabled_packs_produce_sources(self):
        """Default packs (all enabled except design/art) produce subs, feeds, queries."""
        derived = derive_config(DEFAULT_TOPIC_PACKS)
        assert len(derived["sources"]["reddit"]["subreddits"]) > 0
        assert len(derived["sources"]["rss"]["feeds"]) > 0
        assert len(derived["sources"]["github"]["queries"]) > 0
        assert len(derived["topic_boost"]) > 0
        assert len(derived["topic_keywords"]) > 0

    def test_disabled_pack_produces_no_sources(self):
        """design and art are disabled — no subs, feeds, queries, or boost."""
        packs = {name: dict(p) for name, p in DEFAULT_TOPIC_PACKS.items()}
        # Disable all packs except design/art
        for name, pack in packs.items():
            if name not in ("design", "art"):
                packs[name] = {**pack, "enabled": False}
        derived = derive_config(packs)
        assert derived["sources"]["reddit"]["subreddits"] == []
        assert derived["sources"]["rss"]["feeds"] == []
        assert derived["sources"]["github"]["queries"] == []
        assert derived["topic_boost"] == {}
        assert derived["topic_keywords"] == {}

    def test_disabling_gaming_removes_its_subs(self):
        """Disabling gaming removes its subreddits and feeds from derived config."""
        packs = {name: dict(p) for name, p in DEFAULT_TOPIC_PACKS.items()}
        packs["gaming"] = {**packs["gaming"], "enabled": False}
        derived = derive_config(packs)
        subs = derived["sources"]["reddit"]["subreddits"]
        # gaming subs: gaming, Games, GamingLeaksAndRumours
        assert "gaming" not in [s.lower() for s in subs]
        assert "Games" not in subs
        assert "GamingLeaksAndRumours" not in subs
        # gaming feeds: IGN, Eurogamer
        feed_names = [f.get("name") for f in derived["sources"]["rss"]["feeds"]]
        assert "IGN" not in feed_names
        assert "Eurogamer" not in feed_names
        # gaming boost removed
        assert "gaming" not in derived["topic_boost"]

    def test_enabling_design_adds_no_sources_when_empty(self):
        """Enabling design (which has no keywords/subs/feeds) adds nothing."""
        packs = {name: dict(p) for name, p in DEFAULT_TOPIC_PACKS.items()}
        packs["design"] = {**packs["design"], "enabled": True, "boost": 10}
        derived = derive_config(packs)
        # design has no subs/feeds/queries, but its boost is in topic_boost
        assert derived["topic_boost"].get("design") == 10
        # design keywords is empty, so it shouldn't appear in topic_keywords
        assert "design" not in derived["topic_keywords"]
        # No new subs/feeds/queries
        assert derived["sources"]["reddit"]["subreddits"] == derive_config(DEFAULT_TOPIC_PACKS)["sources"]["reddit"]["subreddits"]

    def test_source_topic_map_maps_subreddits(self):
        """source_topic_map maps r/<sub> and feed names to their topic."""
        derived = derive_config(DEFAULT_TOPIC_PACKS)
        assert derived["source_topic_map"]["r/gaming"] == "gaming"
        assert derived["source_topic_map"]["r/LocalLLaMA"] == "ai"
        assert derived["source_topic_map"]["IGN"] == "gaming"

    def test_no_duplicate_subreddits(self):
        """If two packs share a subreddit, it appears once."""
        packs = {
            "a": {"enabled": True, "boost": 10, "keywords": [],
                  "subreddits": ["shared", "alpha"], "feeds": [], "github_queries": []},
            "b": {"enabled": True, "boost": 10, "keywords": [],
                  "subreddits": ["shared", "beta"], "feeds": [], "github_queries": []},
        }
        derived = derive_config(packs)
        assert derived["sources"]["reddit"]["subreddits"] == ["shared", "alpha", "beta"]


# --- Override validation tests ------------------------------------------

class TestTopicOverrides:
    """Verify runtime overrides merge and validate correctly."""

    def test_unknown_topic_name_rejected(self):
        """Unknown topic names are rejected by validation."""
        errors = validate_topic_overrides({"nonexistent": {"enabled": True}})
        assert len(errors) == 1
        assert "nonexistent" in errors[0]

    def test_valid_override_passes(self):
        """Valid topic overrides pass validation."""
        errors = validate_topic_overrides({"gaming": {"enabled": False}})
        assert errors == []

    def test_invalid_pack_field_rejected(self):
        """Invalid pack field is rejected."""
        errors = validate_topic_overrides({"gaming": {"bogus_field": True}})
        assert len(errors) == 1
        assert "bogus_field" in errors[0]

    def test_merge_packs_partial_override(self):
        """Partial override merges over defaults, keeping unset keys."""
        packs = merge_packs({"gaming": {"enabled": False}})
        # gaming is disabled
        assert packs["gaming"]["enabled"] is False
        # but its boost, keywords, subs etc are unchanged
        assert packs["gaming"]["boost"] == DEFAULT_TOPIC_PACKS["gaming"]["boost"]
        assert packs["gaming"]["keywords"] == DEFAULT_TOPIC_PACKS["gaming"]["keywords"]
        # Other packs are unchanged
        assert packs["ai"]["enabled"] is True

    def test_merge_packs_override_boost(self):
        """Override just the boost of a pack."""
        packs = merge_packs({"ai": {"boost": 50}})
        assert packs["ai"]["boost"] == 50
        assert packs["ai"]["enabled"] is True  # unchanged


# --- Scoring: max-not-sum + origin_topic tests --------------------------

class TestScoringChanges:
    """Verify H-2 scoring changes: max bonus, origin topic, no stacking."""

    def _cfg(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "source_weights": DEFAULT_SOURCE_WEIGHTS,
            "topic_boost": dict(DEFAULT_TOPIC_BOOST),
            "topic_keywords": dict(TOPIC_KEYWORDS),
            "source_topic_map": {},
            "lookback_hours": 48,
        }
        base.update(overrides)
        return base

    def test_bonus_is_max_not_sum(self):
        """A title matching multiple topics gets MAX(boost), not sum."""
        # "robot" matches robotics (boost 18); "unity" matches gamedev (boost 15).
        # With sum: 33. With max: 18.
        item = {
            "title": "New robot using unity engine",
            "source": "rss", "source_name": "test",
            "url": "http://x.com",
        }
        cfg = self._cfg()
        bonus = topic_bonus(item, cfg["topic_boost"])
        # max(18, 15) = 18, not 33
        assert bonus == 18

    def test_old_stacked_ai_bonus_gone(self):
        """AI keywords (llm, local llm, coding agent) no longer stack."""
        # "local llm" + "coding agent" + "llm" all match the 'ai' pack.
        # Old: 20+20+25+25 = 90. New: 20 (max, single pack).
        item = {
            "title": "New local LLM coding agent with llama.cpp",
            "source": "rss", "source_name": "test",
            "url": "http://x.com",
        }
        cfg = self._cfg()
        bonus = topic_bonus(item, cfg["topic_boost"])
        assert bonus == 20  # ai pack boost, not stacked

    def test_origin_topic_bonus_without_keywords(self):
        """An r/gaming post titled 'Leaked footage' gets gaming boost
        even though the title has no gaming keywords."""
        cfg = self._cfg(source_topic_map={"r/gaming": "gaming"})
        item = {
            "title": "Leaked footage",
            "source": "reddit",
            "source_name": "r/gaming",
            "url": "http://x.com",
        }
        bd = score_breakdown(item, cfg)
        assert bd["origin_topic"] == "gaming"
        assert bd["topic_bonus"] == 20  # gaming boost
        assert "gaming" in bd["matched_topics"]

    def test_origin_topic_uses_max_not_replaces(self):
        """If both keyword match and origin match, take max(boost)."""
        cfg = self._cfg(source_topic_map={"r/LocalLLaMA": "ai"})
        # "robotics" matches robotics (18); origin is ai (20). max = 20.
        item = {
            "title": "Robotics breakthrough on r/LocalLLaMA",
            "source": "reddit",
            "source_name": "r/LocalLLaMA",
            "url": "http://x.com",
        }
        bd = score_breakdown(item, cfg)
        assert bd["topic_bonus"] == 20  # max(18 keyword, 20 origin)
        assert "ai" in bd["matched_topics"]
        assert "robotics" in bd["matched_topics"]

    def test_origin_topic_none_for_unknown_source(self):
        """A source_name not in the pack table gets no origin bonus."""
        cfg = self._cfg(source_topic_map={"r/gaming": "gaming"})
        item = {
            "title": "Random title",
            "source": "rss",
            "source_name": "Unknown Blog",
            "url": "http://x.com",
        }
        bd = score_breakdown(item, cfg)
        assert bd["origin_topic"] is None
        assert bd["topic_bonus"] == 0

    def test_origin_topic_handles_merged_source_name(self):
        """Merged source_name 'r/gaming + IGN' finds the first match."""
        cfg = self._cfg(source_topic_map={"r/gaming": "gaming", "IGN": "gaming"})
        item = {
            "title": "Leaked footage",
            "source": "reddit",
            "source_name": "r/gaming + IGN",
            "url": "http://x.com",
        }
        bd = score_breakdown(item, cfg)
        assert bd["origin_topic"] == "gaming"

    def test_research_keywords_no_longer_mislabel_science_as_ai(self):
        """H-2 review case: r/science 'Study finds GPU leak' was labelled ai
        because the ai pack held research/paper/study/benchmark words.
        Those words now live in new_research; the ai pack must not match."""
        derived = derive_config(DEFAULT_TOPIC_PACKS)
        cfg = self._cfg(
            topic_boost=dict(derived["topic_boost"]),
            topic_keywords=dict(derived["topic_keywords"]),
            source_topic_map=dict(derived["source_topic_map"]),
        )
        item = {
            "title": "Study finds GPU leak",
            "source": "reddit",
            "source_name": "r/science",
            "url": "http://x.com",
        }
        bd = score_breakdown(item, cfg)
        assert "ai" not in bd["matched_topics"]
        assert bd["origin_topic"] == "science"
        assert "science" in bd["matched_topics"]

    def test_new_research_has_keywords_and_hf_source_mapping(self):
        """new_research owns the research keywords and the HF Papers
        source_name, so its boost 20 can actually fire."""
        derived = derive_config(DEFAULT_TOPIC_PACKS)
        assert "new_research" in derived["topic_keywords"]
        assert "arxiv" in derived["topic_keywords"]["new_research"]
        assert derived["source_topic_map"].get("Hugging Face Papers") == "new_research"
        assert derived["topic_boost"].get("new_research") == 20

    def test_hf_papers_origin_topic_fires_without_keywords(self):
        """An HF Papers item with a keyword-free title still gets
        origin_topic=new_research and its boost via source_names mapping."""
        derived = derive_config(DEFAULT_TOPIC_PACKS)
        cfg = self._cfg(
            topic_boost=dict(derived["topic_boost"]),
            topic_keywords=dict(derived["topic_keywords"]),
            source_topic_map=dict(derived["source_topic_map"]),
        )
        item = {
            "title": "Diffusion transformers at scale",
            "source": "huggingface_papers",
            "source_name": "Hugging Face Papers",
            "url": "https://huggingface.co/papers/2608.00001",
        }
        bd = score_breakdown(item, cfg)
        assert bd["origin_topic"] == "new_research"
        assert bd["topic_bonus"] == 20
        assert "new_research" in bd["matched_topics"]


# --- load_config integration tests --------------------------------------

class TestLoadConfigWithTopics:
    """Verify load_config uses topic packs correctly."""

    class MockSettings:
        def __init__(self, data):
            self._data = data
        def list(self, section):
            return self._data.get(section, {})

    def test_default_config_has_pack_derived_sources(self):
        from newsbot.config import load_config
        cfg = load_config(self.MockSettings({}))
        # Should have reddit subs from packs
        assert len(cfg["sources"]["reddit"]["subreddits"]) > 0
        assert "gaming" in [s.lower() for s in cfg["sources"]["reddit"]["subreddits"]]
        # Should have RSS feeds from packs
        feed_names = [f.get("name") for f in cfg["sources"]["rss"]["feeds"]]
        assert "IGN" in feed_names
        # Should have topic_boost with pack-derived keys
        assert "gaming" in cfg["topic_boost"]
        assert "ai" in cfg["topic_boost"]
        # Should have source_topic_map
        assert cfg["source_topic_map"]["r/gaming"] == "gaming"
        # Should have topic_keywords
        assert "gaming" in cfg["topic_keywords"]

    def test_disabling_pack_via_topics_override(self):
        from newsbot.config import load_config
        settings = self.MockSettings({"news": {"topics": {"gaming": {"enabled": False}}}})
        cfg = load_config(settings)
        subs = [s.lower() for s in cfg["sources"]["reddit"]["subreddits"]]
        assert "gaming" not in subs
        assert "games" not in [s.lower() for s in cfg["sources"]["reddit"]["subreddits"]]
        feed_names = [f.get("name") for f in cfg["sources"]["rss"]["feeds"]]
        assert "IGN" not in feed_names
        assert "gaming" not in cfg["topic_boost"]

    def test_unknown_topic_in_override_rejected(self):
        from newsbot.config import load_config
        settings = self.MockSettings({"news": {"topics": {"bogus": {"enabled": True}}}})
        with pytest.raises(ValueError, match="unknown topic pack"):
            load_config(settings)

    def test_disabled_design_art_produce_no_sources(self):
        from newsbot.config import load_config
        cfg = load_config(self.MockSettings({}))
        # design and art are disabled by default
        assert "design" not in cfg["topic_boost"]
        assert "art" not in cfg["topic_boost"]
        # No design/art subreddits in the source list
        subs = [s.lower() for s in cfg["sources"]["reddit"]["subreddits"]]
        assert "design" not in subs
        assert "art" not in subs

    def test_explicit_sources_override_pack_derived(self):
        from newsbot.config import load_config
        settings = self.MockSettings({"news": {"sources": {
            "reddit": {"subreddits": ["customsub"], "limit": 5}
        }}})
        cfg = load_config(settings)
        # Explicit reddit source replaces pack-derived reddit
        assert cfg["sources"]["reddit"]["subreddits"] == ["customsub"]

    def test_hn_source_present_by_default(self):
        from newsbot.config import load_config
        cfg = load_config(self.MockSettings({}))
        assert "hackernews" in cfg["sources"]
        assert cfg["sources"]["hackernews"]["tags"] == "front_page"
