"""Tests for config validation and topic matching (flow_001031)."""
import pytest
from typing import Any

from newsbot.config import DEFAULT_TOPIC_BOOST
from newsbot.scoring import topic_bonus


class TestTopicMatching:
    """Verify boundary-aware keyword matching for short terms."""

    def test_ai_does_not_match_email(self):
        """'ai' should NOT match 'email' or 'trail'."""
        item = {"title": "New email service launches", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        # Should NOT get the 'ai' boost
        assert bonus < DEFAULT_TOPIC_BOOST["ai"]

    def test_ai_matches_standalone_ai(self):
        """'AI' as a standalone word should match."""
        item = {"title": "New AI model released", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        assert bonus >= DEFAULT_TOPIC_BOOST["ai"]

    def test_vr_does_not_match_everyone(self):
        """'vr' should NOT match substrings."""
        item = {"title": "Everyone is talking about this", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        assert bonus < DEFAULT_TOPIC_BOOST["vr_ar"]

    def test_vr_matches_standalone(self):
        """'VR' as a standalone word should match."""
        item = {"title": "New VR headset announced", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        assert bonus >= DEFAULT_TOPIC_BOOST["vr_ar"]

    def test_ml_does_not_match_html(self):
        """'ml' should NOT match 'html' or 'mill'."""
        item = {"title": "New HTML framework", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        assert bonus < DEFAULT_TOPIC_BOOST["ai"]

    def test_ml_matches_standalone(self):
        """'ML' as a standalone word should match."""
        item = {"title": "New ML benchmark results", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        assert bonus >= DEFAULT_TOPIC_BOOST["ai"]

    def test_multi_word_phrase_substring_match(self):
        """Multi-word phrases like 'language model' should use substring matching."""
        item = {"title": "New language model architecture", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        assert bonus >= DEFAULT_TOPIC_BOOST["llm"]

    def test_long_keyword_substring_match(self):
        """Long keywords (>4 chars) should use substring matching."""
        item = {"title": "New robotics breakthrough", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        assert bonus >= DEFAULT_TOPIC_BOOST["robotics"]

    def test_case_insensitive_matching(self):
        """Matching should be case-insensitive."""
        item_lower = {"title": "new ai model", "snippet": "", "raw_text": ""}
        item_upper = {"title": "NEW AI MODEL", "snippet": "", "raw_text": ""}
        item_mixed = {"title": "New Ai Model", "snippet": "", "raw_text": ""}
        assert topic_bonus(item_lower, DEFAULT_TOPIC_BOOST) == \
               topic_bonus(item_upper, DEFAULT_TOPIC_BOOST) == \
               topic_bonus(item_mixed, DEFAULT_TOPIC_BOOST)


class TestConfigValidation:
    """Verify config validation catches invalid values."""

    def _make_config(self, **overrides) -> dict[str, Any]:
        base = {
            "sources": {},
            "source_weights": {"hn": 1.2, "reddit": 1.0},
            "topic_boost": {},
            "lookback_hours": 48,
            "max_candidates": 20,
            "max_final_news": 8,
            "min_score": 35.0,
            "source_quota": 4,
            "item_prune_hours": 48,
            "llm_temperature": 0.4,
            "llm_max_tokens_filter": 8000,
            "llm_max_tokens_digest": 8000,
            "style_prompt": "",
        }
        base.update(overrides)
        return base

    def test_valid_config_passes(self):
        from newsbot.config import _validate_config
        _validate_config(self._make_config())  # should not raise

    def test_negative_lookback_hours(self):
        from newsbot.config import _validate_config
        with pytest.raises(ValueError, match="lookback_hours"):
            _validate_config(self._make_config(lookback_hours=-1))

    def test_zero_max_candidates(self):
        from newsbot.config import _validate_config
        with pytest.raises(ValueError, match="max_candidates"):
            _validate_config(self._make_config(max_candidates=0))

    def test_max_final_exceeds_max_candidates(self):
        from newsbot.config import _validate_config
        with pytest.raises(ValueError, match="max_final_news.*max_candidates"):
            _validate_config(self._make_config(max_candidates=10, max_final_news=20))

    def test_negative_source_quota(self):
        from newsbot.config import _validate_config
        with pytest.raises(ValueError, match="source_quota"):
            _validate_config(self._make_config(source_quota=-1))

    def test_invalid_llm_temperature(self):
        from newsbot.config import _validate_config
        with pytest.raises(ValueError, match="llm_temperature"):
            _validate_config(self._make_config(llm_temperature=3.0))

    def test_negative_source_weight(self):
        from newsbot.config import _validate_config
        with pytest.raises(ValueError, match="source_weights"):
            _validate_config(self._make_config(source_weights={"hn": -1.0}))

    def test_rss_feed_missing_url(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"rss": {"feeds": [{"name": "No URL"}]}})
        with pytest.raises(ValueError, match="missing 'url'"):
            _validate_config(cfg)

    def test_rss_feed_missing_name(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"rss": {"feeds": [{"url": "http://x.com/rss"}]}})
        with pytest.raises(ValueError, match="missing 'name'"):
            _validate_config(cfg)