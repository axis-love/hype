"""Tests for typed Candidate boundary model (flow_001036)."""
import pytest
from typing import Any

from newsbot.collectors.base import Candidate, new_candidate


class TestCandidate:
    """Verify the typed Candidate model works correctly."""

    def test_required_fields_validated(self):
        """Empty title/source/source_name should raise."""
        with pytest.raises(ValueError, match="title"):
            Candidate(title="", url="http://x.com", source="hn", source_name="HN")
        with pytest.raises(ValueError, match="source"):
            Candidate(title="T", url="http://x.com", source="", source_name="HN")
        with pytest.raises(ValueError, match="source_name"):
            Candidate(title="T", url="http://x.com", source="hn", source_name="")

    def test_default_values(self):
        """Defaults should match the old dict behavior."""
        c = Candidate(title="T", url="http://x.com", source="hn", source_name="HN")
        assert c.score == 0.0
        assert c.crosspost_count == 1
        assert c.penalty == 1.0
        assert c.source_type == "hn"  # auto-set from source
        assert c.upvotes is None
        assert c.stars is None

    def test_to_dict_roundtrip(self):
        """to_dict / from_dict should preserve all fields."""
        original = Candidate(
            title="AI Breakthrough", url="http://x.com/ai", source="hn",
            source_name="Hacker News", upvotes=500, stars=None, score=75.5,
            crosspost_count=3, raw_json={"foo": "bar"},
        )
        d = original.to_dict()
        restored = Candidate.from_dict(d)
        assert restored.title == "AI Breakthrough"
        assert restored.source == "hn"
        assert restored.upvotes == 500
        assert restored.score == 75.5
        assert restored.crosspost_count == 3
        assert restored.raw_json == {"foo": "bar"}

    def test_to_dict_has_all_keys(self):
        """to_dict should include all keys that downstream code expects."""
        c = Candidate(title="T", url="U", source="hn", source_name="HN")
        d = c.to_dict()
        expected_keys = {
            "title", "url", "source", "source_name", "source_type",
            "snippet", "published_at", "score", "upvotes", "comments",
            "stars", "forks", "reposts", "upvote_ratio", "velocity",
            "category", "raw_text", "extracted_text", "crosspost_count",
            "raw_json", "candidate_id", "importance", "reason",
            "short_summary", "penalty",
        }
        assert expected_keys.issubset(set(d.keys())), \
            f"Missing keys: {expected_keys - set(d.keys())}"


class TestNewCandidateCompat:
    """Verify new_candidate() backward compatibility."""

    def test_new_candidate_returns_dict_with_extra_fields(self):
        """new_candidate should accept extra fields like the old version."""
        d = new_candidate(
            title="T", url="http://x.com", source="hn", source_name="HN",
            upvotes=100, stars=50, raw_json={"test": True},
        )
        assert d["title"] == "T"
        assert d["source"] == "hn"
        assert d["upvotes"] == 100
        assert d["stars"] == 50
        assert d["raw_json"] == {"test": True}
        assert d["score"] == 0.0
        assert d["crosspost_count"] == 1

    def test_new_candidate_validates_required(self):
        """new_candidate should validate via Candidate.__post_init__."""
        with pytest.raises(ValueError):
            new_candidate(title="", url="", source="hn", source_name="HN")


class TestCollectorCompat:
    """Verify existing collectors still work with the new model."""

    def test_dedupe_with_candidate_dict(self):
        """Dedupe should work with dicts produced by new_candidate."""
        from newsbot.dedupe import dedupe_and_merge
        a = new_candidate(title="Story", url="https://example.com/s",
                          source="hn", source_name="HN", upvotes=100)
        b = new_candidate(title="Story", url="https://example.com/s",
                          source="reddit", source_name="Reddit", upvotes=200)
        out = dedupe_and_merge([a, b])
        assert len(out) == 1
        assert out[0]["crosspost_count"] == 2
        assert out[0]["upvotes"] == 300

    def test_scoring_with_candidate_dict(self):
        """Scoring should work with dicts produced by new_candidate."""
        from newsbot.config import DEFAULT_SOURCE_WEIGHTS
        from newsbot.scoring import score_all
        items = [
            new_candidate(title="zzz neutral", url="http://x.com",
                          source="hn", source_name="HN", upvotes=100, comments=10),
        ]
        cfg = {"source_weights": DEFAULT_SOURCE_WEIGHTS, "topic_boost": {}, "lookback_hours": 48}
        scored = score_all(items, cfg)
        assert "score" in scored[0]
        assert scored[0]["score"] > 0

    def test_candidate_from_dict_handles_missing_fields(self):
        """from_dict should handle dicts with missing optional fields."""
        d = {"title": "T", "url": "U", "source": "hn", "source_name": "HN"}
        c = Candidate.from_dict(d)
        assert c.title == "T"
        assert c.upvotes is None
        assert c.score == 0.0