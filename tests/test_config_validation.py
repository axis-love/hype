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
        assert bonus >= DEFAULT_TOPIC_BOOST["ai"]

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
        bl = topic_bonus(item_lower, DEFAULT_TOPIC_BOOST)
        bu = topic_bonus(item_upper, DEFAULT_TOPIC_BOOST)
        bm = topic_bonus(item_mixed, DEFAULT_TOPIC_BOOST)
        assert bl == bu == bm


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

    def test_non_dict_rss_config(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"rss": "not-a-dict"})
        with pytest.raises(ValueError, match="sources.rss must be a dict"):
            _validate_config(cfg)

    def test_non_list_rss_feeds(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"rss": {"feeds": "not-a-list"}})
        with pytest.raises(ValueError, match="sources.rss.feeds must be a list"):
            _validate_config(cfg)

    def test_rss_feed_non_numeric_weight(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"rss": {"feeds": [
            {"url": "http://x.com/rss", "name": "test", "weight": "heavy"}
        ]}})
        with pytest.raises(ValueError, match="weight must be numeric"):
            _validate_config(cfg)

    def test_non_dict_reddit_config(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"reddit": "bad"})
        with pytest.raises(ValueError, match="sources.reddit must be a dict"):
            _validate_config(cfg)

    def test_non_list_subreddits(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"reddit": {"subreddits": "all"}})
        with pytest.raises(ValueError, match="subreddits must be a list"):
            _validate_config(cfg)

    def test_non_dict_github_config(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"github": 42})
        with pytest.raises(ValueError, match="sources.github must be a dict"):
            _validate_config(cfg)

    def test_non_list_github_queries(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"github": {"queries": "llm"}})
        with pytest.raises(ValueError, match="sources.github.queries must be a list"):
            _validate_config(cfg)

    def test_non_int_max_candidates(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(max_candidates="twenty")
        with pytest.raises(ValueError, match="max_candidates must be int"):
            _validate_config(cfg)


class TestLLMEnvValidation:
    """Verify LLM env validation at startup (flow_001031 round 1)."""

    def test_missing_lm_base_raises(self):
        from newsbot.main import _validate_llm_env
        from unittest.mock import patch
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="LM_BASE is not set"):
                _validate_llm_env()

    def test_missing_lm_model_raises(self):
        from newsbot.main import _validate_llm_env
        from unittest.mock import patch
        with patch.dict("os.environ", {"LM_BASE": "http://x.com/v1"}, clear=True):
            with pytest.raises(RuntimeError, match="LM_MODEL is not set"):
                _validate_llm_env()

    def test_both_set_passes(self):
        from newsbot.main import _validate_llm_env
        from unittest.mock import patch
        with patch.dict("os.environ", {"LM_BASE": "http://x.com/v1", "LM_MODEL": "test", "LM_API_KEY": "sk-test"}, clear=True):
            _validate_llm_env()  # should not raise

    def test_missing_lm_api_key_raises(self):
        from newsbot.main import _validate_llm_env
        from unittest.mock import patch
        with patch.dict("os.environ", {"LM_BASE": "http://x.com/v1", "LM_MODEL": "test"}, clear=True):
            with pytest.raises(RuntimeError, match="LM_API_KEY is not set"):
                _validate_llm_env()


class TestMalformedConfigRaises:
    """Malformed values must raise ValueError, not silently fall back to defaults."""

    class MockSettings:
        def __init__(self, data):
            self._data = data
        def list(self, section):
            return self._data.get(section, {})

    def test_malformed_max_candidates_raises(self):
        from newsbot.config import load_config
        settings = self.MockSettings({"news": {"max_candidates": "not-an-int"}})
        with pytest.raises(ValueError, match="max_candidates must be int"):
            load_config(settings)

    def test_malformed_sources_raises(self):
        from newsbot.config import load_config
        settings = self.MockSettings({"news": {"sources": "not-a-dict"}})
        with pytest.raises(ValueError, match="sources must be a dict"):
            load_config(settings)

    def test_malformed_source_weights_raises(self):
        from newsbot.config import load_config
        settings = self.MockSettings({"news": {"source_weights": "not-a-dict"}})
        with pytest.raises(ValueError, match="source_weights must be a dict"):
            load_config(settings)

    def test_malformed_topic_boost_raises(self):
        from newsbot.config import load_config
        settings = self.MockSettings({"news": {"topic_boost": "not-a-dict"}})
        with pytest.raises(ValueError, match="topic_boost must be a dict"):
            load_config(settings)

    def test_missing_values_use_defaults(self):
        from newsbot.config import load_config
        settings = self.MockSettings({})
        cfg = load_config(settings)  # should not raise — all defaults
        assert cfg["max_candidates"] == 20
        assert cfg["lookback_hours"] == 48


class TestTopicMatchingBoundary:
    """Verify single-word keywords use word-boundary matching."""

    def test_unity_does_not_match_community(self):
        """'unity' should NOT match 'community'."""
        item = {"title": "Community platform launches", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        # Should NOT get the 'gamedev' boost (unity is a gamedev keyword now)
        assert bonus < DEFAULT_TOPIC_BOOST["gamedev"]

    def test_ar_does_not_match_article(self):
        """'ar' should NOT match 'article'."""
        item = {"title": "Article about new tech", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        assert bonus < DEFAULT_TOPIC_BOOST["vr_ar"]

    def test_unity_matches_standalone(self):
        """'Unity' as a standalone word should match."""
        item = {"title": "Unity 6 released", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        assert bonus >= DEFAULT_TOPIC_BOOST["gamedev"]

    def test_long_single_word_boundary(self):
        """Longer single words like 'robot' should use word-boundary too."""
        # "robot" should match "robot", not "robotics" (different boost key)
        item = {"title": "New robot arm", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        assert bonus >= DEFAULT_TOPIC_BOOST["robotics"]

    def test_multi_word_phrase_still_substring(self):
        """Multi-word phrases should still use substring matching."""
        item = {"title": "New language model architecture", "snippet": "", "raw_text": ""}
        bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
        assert bonus >= DEFAULT_TOPIC_BOOST["ai"]


class TestNestedListValidation:
    """Verify nested list member types are validated."""

    def _make_config(self, **overrides) -> dict[str, Any]:
        base = {
            "sources": {}, "source_weights": {"hn": 1.2, "reddit": 1.0},
            "topic_boost": {}, "lookback_hours": 48, "max_candidates": 20,
            "max_final_news": 8, "min_score": 35.0, "source_quota": 4,
            "item_prune_hours": 48, "llm_temperature": 0.4,
            "llm_max_tokens_filter": 8000, "llm_max_tokens_digest": 8000,
            "style_prompt": "",
        }
        base.update(overrides)
        return base

    def test_rss_feed_entry_not_dict(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"rss": {"feeds": ["not-a-dict"]}})
        with pytest.raises(ValueError, match="rss.feeds.*must be a dict"):
            _validate_config(cfg)

    def test_reddit_subreddit_not_string(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"reddit": {"subreddits": [123, "valid"]}})
        with pytest.raises(ValueError, match="subreddits.*must be a string"):
            _validate_config(cfg)

    def test_github_query_not_string(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"github": {"queries": [42]}})
        with pytest.raises(ValueError, match="queries.*must be a string"):
            _validate_config(cfg)

    def test_hn_query_not_string(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"hackernews": {"queries": [42]}})
        with pytest.raises(ValueError, match="queries.*must be a string"):
            _validate_config(cfg)

    def test_ph_topic_not_string(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"producthunt": {"topics": [123]}})
        with pytest.raises(ValueError, match="topics.*must be a string"):
            _validate_config(cfg)

    def test_unknown_source_rejected(self):
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"twitter": {"limit": 10}})
        with pytest.raises(ValueError, match="unknown source"):
            _validate_config(cfg)

    def test_nan_source_weight_rejected(self):
        import math
        from newsbot.config import _validate_config
        cfg = self._make_config(source_weights={"hn": float("nan")})
        with pytest.raises(ValueError, match="must be finite"):
            _validate_config(cfg)

    def test_nan_rss_feed_weight_rejected(self):
        import math
        from newsbot.config import _validate_config
        cfg = self._make_config(sources={"rss": {"feeds": [
            {"url": "http://x.com/rss", "name": "test", "weight": float("nan")}
        ]}})
        with pytest.raises(ValueError, match="must be finite"):
            _validate_config(cfg)


class TestRuntimeEnvValidation:
    """Verify numeric env vars are validated at startup."""

    def test_invalid_lm_timeout_rejected(self):
        from newsbot.main import _validate_llm_env
        from unittest.mock import patch
        with patch.dict("os.environ", {
            "LM_BASE": "http://x.com/v1", "LM_MODEL": "test", "LM_API_KEY": "sk-test",
            "LM_TIMEOUT": "not-a-number",
        }, clear=True):
            with pytest.raises(RuntimeError, match="LM_TIMEOUT must be numeric"):
                _validate_llm_env()

    def test_negative_lm_timeout_rejected(self):
        from newsbot.main import _validate_llm_env
        from unittest.mock import patch
        with patch.dict("os.environ", {
            "LM_BASE": "http://x.com/v1", "LM_MODEL": "test", "LM_API_KEY": "sk-test",
            "LM_TIMEOUT": "-5",
        }, clear=True):
            with pytest.raises(RuntimeError, match="LM_TIMEOUT must be positive"):
                _validate_llm_env()

    def test_non_numeric_admin_user_id_rejected(self):
        from newsbot.main import _validate_llm_env
        from unittest.mock import patch
        with patch.dict("os.environ", {
            "LM_BASE": "http://x.com/v1", "LM_MODEL": "test", "LM_API_KEY": "sk-test",
            "ADMIN_USER_ID": "not-numeric",
        }, clear=True):
            with pytest.raises(RuntimeError, match="ADMIN_USER_ID must be numeric"):
                _validate_llm_env()