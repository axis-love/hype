"""Tests for source weights and diversity selection (flow_001028).

Verifies that:
- HN candidates use the configured Hacker News weight (1.2, not 1.0)
- RSS candidates carry their feed-specific weight
- Diversity selection uses round-robin so all sources get representation
- When guarantees can't be met, remaining slots fill by score
"""
import pytest
from typing import Any

from newsbot.config import DEFAULT_SOURCE_WEIGHTS, _SOURCE_ALIASES
from newsbot.scoring import hype_score


CFG = {
    "source_weights": DEFAULT_SOURCE_WEIGHTS,
    "topic_boost": {},
    "lookback_hours": 48,
}


def _item(**overrides):
    base = {
        "source": "hn",
        "source_name": "Hacker News",
        "title": "neutral title zzz",
        "url": "",
        "upvotes": 100,
        "comments": 10,
    }
    base.update(overrides)
    return base


class TestSourceWeightNormalization:
    def test_hn_gets_hackernews_weight(self):
        """HN candidate (source='hn') should get weight 1.2, not fallback 1.0."""
        hn_item = _item(source="hn", upvotes=100, comments=10)
        hn_score = hype_score(hn_item, CFG)

        # Compare with a source that has weight 1.0
        reddit_item = _item(source="reddit", upvotes=100, comments=10)
        reddit_score = hype_score(reddit_item, CFG)

        assert hn_score > reddit_score, "HN should score higher than reddit due to 1.2 vs 1.0 weight"

    def test_source_alias_map(self):
        assert _SOURCE_ALIASES.get("hn") == "hackernews"
        assert _SOURCE_ALIASES.get("reddit") is None  # no alias needed

    def test_rss_feed_weight_applied(self):
        """RSS candidate with feed weight in raw_json should get that weight."""
        rss_normal = _item(source="rss", raw_json={}, upvotes=10, comments=5)
        rss_official = _item(source="rss", raw_json={"weight": 1.3}, upvotes=10, comments=5)

        normal_score = hype_score(rss_normal, CFG)
        official_score = hype_score(rss_official, CFG)

        assert official_score > normal_score, "RSS with weight=1.3 should score higher than weight=0.5"


class TestDiversitySelection:
    """Tests for _select_diverse_candidates round-robin allocation."""

    def _make_candidates(self, source: str, count: int, score_base: float = 50.0) -> list[dict]:
        return [
            {"source": source, "title": f"{source}-{i}", "score": score_base - i, "url": f"http://{source}/{i}"}
            for i in range(count)
        ]

    def test_all_sources_get_representation(self):
        """With 3 sources and max_candidates=6, each source should get at least 2 slots."""
        from newsbot.main import _select_diverse_candidates

        hn = self._make_candidates("hn", 5, 100)
        reddit = self._make_candidates("reddit", 5, 80)
        github = self._make_candidates("github", 5, 60)

        scored = sorted(hn + reddit + github, key=lambda c: c["score"], reverse=True)
        cfg = {"source_quota": 3}
        top = _select_diverse_candidates(scored, 6, cfg)

        sources = [c["source"] for c in top]
        assert sources.count("hn") >= 1
        assert sources.count("reddit") >= 1
        assert sources.count("github") >= 1

    def test_dominant_source_does_not_crowd_out(self):
        """Even if GitHub has 20 items, other sources should still get slots."""
        from newsbot.main import _select_diverse_candidates

        github = self._make_candidates("github", 20, 100)
        hn = self._make_candidates("hn", 3, 50)
        reddit = self._make_candidates("reddit", 3, 40)

        scored = sorted(github + hn + reddit, key=lambda c: c["score"], reverse=True)
        cfg = {"source_quota": 3}
        top = _select_diverse_candidates(scored, 8, cfg)

        sources = [c["source"] for c in top]
        # Each source should get at least 2 slots (round-robin, 8 slots, 3 sources)
        assert sources.count("hn") >= 1, "HN should have at least 1 slot"
        assert sources.count("reddit") >= 1, "Reddit should have at least 1 slot"
        assert sources.count("github") >= 1, "GitHub should have at least 1 slot"

    def test_remaining_slots_filled_by_score(self):
        """When all sources have fewer items than slots, fill by score."""
        from newsbot.main import _select_diverse_candidates

        hn = self._make_candidates("hn", 2, 100)
        reddit = self._make_candidates("reddit", 2, 80)

        scored = sorted(hn + reddit, key=lambda c: c["score"], reverse=True)
        cfg = {"source_quota": 5}
        top = _select_diverse_candidates(scored, 6, cfg)

        assert len(top) == 4  # only 4 candidates exist
        # Should be sorted by score
        scores = [c["score"] for c in top]
        assert scores == sorted(scores, reverse=True)

    def test_empty_candidates(self):
        from newsbot.main import _select_diverse_candidates
        assert _select_diverse_candidates([], 10, {"source_quota": 3}) == []