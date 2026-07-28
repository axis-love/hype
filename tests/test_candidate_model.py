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

    def test_new_candidate_warns_unknown_field(self):
        """Unknown extra fields (typos) should raise ValueError."""
        with pytest.raises(ValueError, match="upvoets"):
            new_candidate(
                title="T", url="http://x.com", source="hn", source_name="HN",
                upvoets=100,  # typo: "upvoets" instead of "upvotes"
            )


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


class TestCandidateValidation:
    """Verify Candidate field validation."""

    def test_empty_url_rejected(self):
        """Empty URL should raise ValueError."""
        with pytest.raises(ValueError, match="url"):
            Candidate(title="T", url="", source="hn", source_name="HN")

    def test_negative_engagement_rejected(self):
        """Negative engagement values should raise."""
        with pytest.raises(ValueError, match="upvotes"):
            Candidate(title="T", url="U", source="hn", source_name="HN", upvotes=-1)
        with pytest.raises(ValueError, match="comments"):
            Candidate(title="T", url="U", source="hn", source_name="HN", comments=-5)
        with pytest.raises(ValueError, match="stars"):
            Candidate(title="T", url="U", source="hn", source_name="HN", stars=-10)

    def test_negative_score_rejected(self):
        with pytest.raises(ValueError, match="score"):
            Candidate(title="T", url="U", source="hn", source_name="HN", score=-1.0)

    def test_upvote_ratio_range(self):
        """upvote_ratio must be in [0, 1]."""
        with pytest.raises(ValueError, match="upvote_ratio"):
            Candidate(title="T", url="U", source="hn", source_name="HN", upvote_ratio=1.5)
        with pytest.raises(ValueError, match="upvote_ratio"):
            Candidate(title="T", url="U", source="hn", source_name="HN", upvote_ratio=-0.1)
        # Valid values should work
        Candidate(title="T", url="U", source="hn", source_name="HN", upvote_ratio=0.85)

    def test_new_candidate_validates_extra_fields(self):
        """new_candidate should raise on invalid engagement values (no catch-and-continue)."""
        with pytest.raises(ValueError, match="upvotes"):
            new_candidate(
                title="T", url="U", source="hn", source_name="HN",
                upvotes=-5,  # negative engagement should raise
            )

    def test_penalty_zero_roundtrip(self):
        """penalty=0 should round-trip through to_dict/from_dict correctly."""
        c = Candidate(title="T", url="U", source="hn", source_name="HN", penalty=0.0)
        d = c.to_dict()
        assert d["penalty"] == 0.0
        restored = Candidate.from_dict(d)
        assert restored.penalty == 0.0  # should NOT become 1.0


class TestCollectorCandidateCompat:
    """Verify each collector's output round-trips through Candidate.from_dict()."""

    def test_hn_candidate_roundtrip(self):
        """HN collector output → Candidate.from_dict → to_dict preserves fields."""
        from newsbot.collectors.base import new_candidate
        d = new_candidate(
            title="New AI Framework",
            url="https://example.com/ai",
            source="hn", source_name="Hacker News",
            snippet="A new framework for AI",
            published_at="2026-07-15T10:00:00+00:00",
            upvotes=430, comments=180,
            raw_json={"objectID": "42"},
        )
        c = Candidate.from_dict(d)
        assert c.title == "New AI Framework"
        assert c.source == "hn"
        assert c.upvotes == 430
        assert c.comments == 180
        assert c.published_at == "2026-07-15T10:00:00+00:00"
        # Round-trip back to dict
        d2 = c.to_dict()
        assert d2["upvotes"] == 430
        assert d2["comments"] == 180

    def test_reddit_candidate_roundtrip(self):
        """Reddit collector output → Candidate.from_dict → to_dict preserves fields."""
        from newsbot.collectors.base import new_candidate
        d = new_candidate(
            title="Local LLM discussion",
            url="https://reddit.com/r/LocalLLaMA/comments/abc",
            source="reddit", source_name="r/LocalLLaMA",
            snippet="Discussion about local LLMs",
            published_at="2026-07-15T12:00:00+00:00",
            upvotes=2100, comments=340, upvote_ratio=0.92,
            raw_json={"permalink": "/r/LocalLLaMA/comments/abc"},
        )
        c = Candidate.from_dict(d)
        assert c.source == "reddit"
        assert c.source_name == "r/LocalLLaMA"
        assert c.upvotes == 2100
        assert c.upvote_ratio == 0.92
        d2 = c.to_dict()
        assert d2["upvote_ratio"] == 0.92

    def test_github_candidate_roundtrip(self):
        """GitHub collector output → Candidate.from_dict → to_dict preserves fields."""
        from newsbot.collectors.base import new_candidate
        d = new_candidate(
            title="user/awesome-repo",
            url="https://github.com/user/awesome-repo",
            source="github", source_name="GitHub Trending",
            snippet="An awesome repository",
            published_at="2026-07-10T00:00:00+00:00",
            stars=5000, forks=200,
            raw_json={"stargazers_count": 5000, "forks_count": 200},
        )
        d["penalty"] = 0.5  # GitHub sets penalty post-hoc
        c = Candidate.from_dict(d)
        assert c.source == "github"
        assert c.stars == 5000
        assert c.forks == 200
        assert c.penalty == 0.5
        d2 = c.to_dict()
        assert d2["stars"] == 5000
        assert d2["forks"] == 200
        assert d2["penalty"] == 0.5

    def test_rss_candidate_roundtrip(self):
        """RSS collector output → Candidate.from_dict → to_dict preserves fields."""
        from newsbot.collectors.base import new_candidate
        d = new_candidate(
            title="OpenAI Blog Post",
            url="https://openai.com/blog/something",
            source="rss", source_name="OpenAI Blog",
            snippet="A new blog post from OpenAI",
            published_at="2026-07-15T09:00:00+00:00",
            raw_json={"weight": 1.5, "id": "post-123"},
        )
        c = Candidate.from_dict(d)
        assert c.source == "rss"
        assert c.source_name == "OpenAI Blog"
        assert c.raw_json == {"weight": 1.5, "id": "post-123"}
        d2 = c.to_dict()
        assert d2["raw_json"]["weight"] == 1.5

    def test_summarizer_candidate_compat(self):
        """Summarizer output (candidate_id, importance, reason, short_summary) round-trips."""
        from newsbot.collectors.base import new_candidate
        d = new_candidate(
            title="AI Breakthrough",
            url="https://example.com/breakthrough",
            source="hn", source_name="Hacker News",
            upvotes=500,
        )
        # Simulate summarizer adding fields
        d["candidate_id"] = "flow_001036_001"
        d["importance"] = 8
        d["reason"] = "Major breakthrough in AI efficiency"
        d["short_summary"] = "New method reduces training cost by 10x"
        d["extracted_text"] = "Researchers at XYZ lab have developed..."

        c = Candidate.from_dict(d)
        assert c.candidate_id == "flow_001036_001"
        assert c.importance == 8
        assert c.reason == "Major breakthrough in AI efficiency"
        assert c.short_summary == "New method reduces training cost by 10x"
        assert c.extracted_text == "Researchers at XYZ lab have developed..."
        d2 = c.to_dict()
        assert d2["candidate_id"] == "flow_001036_001"
        assert d2["importance"] == 8


class TestSourceIdValidation:
    """Verify source identifier validation and alias normalization."""

    def test_known_sources_accepted(self):
        """All known source IDs should be accepted without error."""
        for src in ("hn", "reddit", "github", "producthunt", "rss", "huggingface_papers"):
            c = Candidate(title="T", url="U", source=src, source_name="N")
            assert c.source == src

    def test_hackernews_alias_normalized_to_hn(self):
        """'hackernews' should be normalized to 'hn'."""
        c = Candidate(title="T", url="U", source="hackernews", source_name="HN")
        assert c.source == "hn"

    def test_unknown_source_rejected(self):
        """Unknown source IDs should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown.*source"):
            Candidate(title="T", url="U", source="twitter", source_name="Twitter")

    def test_empty_source_rejected(self):
        """Empty source should raise ValueError."""
        with pytest.raises(ValueError, match="source"):
            Candidate(title="T", url="U", source="", source_name="N")

    def test_new_candidate_rejects_unknown_source(self):
        """new_candidate should reject unknown source IDs."""
        with pytest.raises(ValueError, match="Unknown.*source"):
            new_candidate(title="T", url="U", source="myspace", source_name="MySpace")

    def test_from_dict_normalizes_source(self):
        """from_dict should normalize source aliases."""
        d = {"title": "T", "url": "U", "source": "hackernews", "source_name": "HN"}
        c = Candidate.from_dict(d)
        assert c.source == "hn"


class TestNumericTypeValidation:
    """Verify numeric field type validation."""

    def test_string_engagement_rejected(self):
        """String engagement values should raise ValueError."""
        with pytest.raises(ValueError, match="must be numeric"):
            Candidate(title="T", url="U", source="hn", source_name="HN", upvotes="100")

    def test_nan_engagement_rejected(self):
        """NaN engagement values should raise ValueError."""
        with pytest.raises(ValueError, match="must be finite"):
            Candidate(title="T", url="U", source="hn", source_name="HN",
                      upvotes=float("nan"))

    def test_inf_engagement_rejected(self):
        """Infinity engagement values should raise ValueError."""
        with pytest.raises(ValueError, match="must be finite"):
            Candidate(title="T", url="U", source="hn", source_name="HN",
                      upvotes=float("inf"))


class TestZeroValuePreservation:
    """Verify that valid zero values are preserved through round-trips."""

    def test_score_zero_roundtrip(self):
        """score=0 should round-trip without becoming default."""
        c = Candidate(title="T", url="U", source="hn", source_name="HN", score=0.0)
        d = c.to_dict()
        assert d["score"] == 0.0
        restored = Candidate.from_dict(d)
        assert restored.score == 0.0

    def test_crosspost_count_zero_roundtrip(self):
        """crosspost_count=0 should round-trip without becoming 1."""
        c = Candidate(title="T", url="U", source="hn", source_name="HN", crosspost_count=0)
        d = c.to_dict()
        assert d["crosspost_count"] == 0
        restored = Candidate.from_dict(d)
        assert restored.crosspost_count == 0

    def test_engagement_zero_accepted(self):
        """Zero engagement values should be accepted (not None)."""
        c = Candidate(title="T", url="U", source="hn", source_name="HN",
                      upvotes=0, comments=0, stars=0)
        assert c.upvotes == 0
        assert c.comments == 0
        assert c.stars == 0


class TestToDictFromDictRoundTrip:
    """Verify to_dict/from_dict round-trips every declared field."""

    def test_full_roundtrip_all_fields(self):
        """Every declared field should survive a to_dict/from_dict round-trip."""
        original = Candidate(
            title="Test", url="https://example.com", source="hn",
            source_name="HN", source_type="hn",
            snippet="A snippet", published_at="2026-07-15T10:00:00+00:00",
            score=42.5, upvotes=100, comments=50, stars=200, forks=30,
            reposts=5, upvote_ratio=0.85, velocity=1.5,
            category="AI", raw_text="raw", extracted_text="extracted",
            crosspost_count=3, raw_json={"key": "value"},
            candidate_id="c001", importance=8, reason="Important",
            short_summary="Short", penalty=0.5,
            contributing_sources=["hn", "reddit"],
        )
        d = original.to_dict()
        restored = Candidate.from_dict(d)
        assert restored.title == "Test"
        assert restored.url == "https://example.com"
        assert restored.source == "hn"
        assert restored.source_name == "HN"
        assert restored.source_type == "hn"
        assert restored.snippet == "A snippet"
        assert restored.published_at == "2026-07-15T10:00:00+00:00"
        assert restored.score == 42.5
        assert restored.upvotes == 100
        assert restored.comments == 50
        assert restored.stars == 200
        assert restored.forks == 30
        assert restored.reposts == 5
        assert restored.upvote_ratio == 0.85
        assert restored.velocity == 1.5
        assert restored.category == "AI"
        assert restored.raw_text == "raw"
        assert restored.extracted_text == "extracted"
        assert restored.crosspost_count == 3
        assert restored.raw_json == {"key": "value"}
        assert restored.candidate_id == "c001"
        assert restored.importance == 8
        assert restored.reason == "Important"
        assert restored.short_summary == "Short"
        assert restored.penalty == 0.5
        assert restored.contributing_sources == ["hn", "reddit"]

    def test_to_dict_does_not_share_mutable_state(self):
        """to_dict should return a fresh dict, not share mutable references."""
        c = Candidate(title="T", url="U", source="hn", source_name="HN",
                      raw_json={"k": "v"}, contributing_sources=["hn"])
        d1 = c.to_dict()
        d1["raw_json"]["k"] = "modified"
        d1["contributing_sources"].append("reddit")
        d2 = c.to_dict()
        assert d2["raw_json"] == {"k": "v"}, "raw_json should not be shared"
        assert d2["contributing_sources"] == ["hn"], "contributing_sources should not be shared"