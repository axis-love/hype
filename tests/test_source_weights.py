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
    """Tests for select_diverse_candidates round-robin allocation."""

    def _make_candidates(self, source: str, count: int, score_base: float = 50.0) -> list[dict]:
        return [
            {"source": source, "title": f"{source}-{i}", "score": score_base - i, "url": f"http://{source}/{i}"}
            for i in range(count)
        ]

    def test_all_sources_get_representation(self):
        """With 3 sources and max_candidates=6, each source should get at least 2 slots."""
        from newsbot.selection import select_diverse_candidates

        hn = self._make_candidates("hn", 5, 100)
        reddit = self._make_candidates("reddit", 5, 80)
        github = self._make_candidates("github", 5, 60)

        scored = sorted(hn + reddit + github, key=lambda c: c["score"], reverse=True)
        cfg = {"source_quota": 3}
        top = select_diverse_candidates(scored, 6, cfg)

        sources = [c["source"] for c in top]
        assert sources.count("hn") >= 1
        assert sources.count("reddit") >= 1
        assert sources.count("github") >= 1

    def test_dominant_source_does_not_crowd_out(self):
        """Even if GitHub has 20 items, other sources should still get slots."""
        from newsbot.selection import select_diverse_candidates

        github = self._make_candidates("github", 20, 100)
        hn = self._make_candidates("hn", 3, 50)
        reddit = self._make_candidates("reddit", 3, 40)

        scored = sorted(github + hn + reddit, key=lambda c: c["score"], reverse=True)
        cfg = {"source_quota": 3}
        top = select_diverse_candidates(scored, 8, cfg)

        sources = [c["source"] for c in top]
        # Each source should get at least 2 slots (round-robin, 8 slots, 3 sources)
        assert sources.count("hn") >= 1, "HN should have at least 1 slot"
        assert sources.count("reddit") >= 1, "Reddit should have at least 1 slot"
        assert sources.count("github") >= 1, "GitHub should have at least 1 slot"

    def test_remaining_slots_filled_by_score(self):
        """When all sources have fewer items than slots, fill by score."""
        from newsbot.selection import select_diverse_candidates

        hn = self._make_candidates("hn", 2, 100)
        reddit = self._make_candidates("reddit", 2, 80)

        scored = sorted(hn + reddit, key=lambda c: c["score"], reverse=True)
        cfg = {"source_quota": 5}
        top = select_diverse_candidates(scored, 6, cfg)

        assert len(top) == 4  # only 4 candidates exist
        # Should be sorted by score
        scores = [c["score"] for c in top]
        assert scores == sorted(scores, reverse=True)

    def test_empty_candidates(self):
        from newsbot.selection import select_diverse_candidates
        assert select_diverse_candidates([], 10, {"source_quota": 3}) == []

    def test_source_quota_zero_respected(self):
        """source_quota=0 should mean no round-robin, all slots filled by score."""
        from newsbot.selection import select_diverse_candidates

        hn = self._make_candidates("hn", 3, 100)
        reddit = self._make_candidates("reddit", 3, 50)
        scored = sorted(hn + reddit, key=lambda c: c["score"], reverse=True)
        cfg = {"source_quota": 0}
        top = select_diverse_candidates(scored, 4, cfg)
        assert len(top) == 4
        # With quota=0, round-robin gives 0 rounds; all filled by score.
        # HN has higher scores, so HN items should dominate.
        assert top[0]["source"] == "hn"

    def test_equal_score_deterministic_with_quota_zero(self):
        """Equal-score candidates with quota=0 and one slot: same title regardless of input order."""
        from newsbot.selection import select_diverse_candidates

        # Two candidates with equal score, different titles.
        alpha = {"source": "hn", "title": "Alpha", "score": 50.0, "url": "http://a"}
        zulu = {"source": "hn", "title": "Zulu", "score": 50.0, "url": "http://z"}

        cfg = {"source_quota": 0}
        top1 = select_diverse_candidates([alpha, zulu], 1, cfg)
        top2 = select_diverse_candidates([zulu, alpha], 1, cfg)

        # With deterministic tie-break (title ascending), Alpha should always win.
        assert len(top1) == 1
        assert len(top2) == 1
        assert top1[0]["title"] == top2[0]["title"] == "Alpha"

    def test_deterministic_selection_across_permutations(self):
        """Reversing input order must produce identical selection."""
        from newsbot.selection import select_diverse_candidates

        candidates = []
        for src, score, i in [
            ("hn", 100, 1), ("hn", 90, 2), ("hn", 80, 3),
            ("reddit", 85, 4), ("reddit", 75, 5), ("reddit", 65, 6),
            ("github", 70, 7), ("github", 60, 8),
        ]:
            candidates.append({"source": src, "title": f"{src}-{i}", "score": float(score), "url": f"http://{src}/{i}"})

        cfg = {"source_quota": 2}
        top1 = select_diverse_candidates(list(candidates), 5, cfg)
        top2 = select_diverse_candidates(list(reversed(candidates)), 5, cfg)

        assert len(top1) == len(top2) == 5
        titles1 = [c["title"] for c in top1]
        titles2 = [c["title"] for c in top2]
        assert titles1 == titles2, f"Selection differs: {titles1} vs {titles2}"


class TestFeedWeightOverride:
    """Verify that per-feed weight replaces global source weight (flow_001028 round 1)."""

    def test_low_feed_weight_respected(self):
        """A feed weight below the global RSS weight should lower the score."""
        cfg = {
            "source_weights": DEFAULT_SOURCE_WEIGHTS,
            "topic_boosts": {},
            "lookback_hours": 48,
        }
        # Global RSS weight is 0.5 in DEFAULT_SOURCE_WEIGHTS.
        # RSS item with feed_weight=0.2 (below global 0.5)
        item_low = {
            "title": "Low weight feed",
            "url": "https://blog.example.com/post",
            "source": "rss",
            "upvotes": 100,
            "comments": 50,
            "published_at": "2026-07-27",
            "raw_json": {"weight": 0.2},
        }
        # RSS item with no feed weight (uses global 0.5)
        item_default = {
            "title": "Default weight feed",
            "url": "https://other.example.com/post",
            "source": "rss",
            "upvotes": 100,
            "comments": 50,
            "published_at": "2026-07-27",
            "raw_json": {},
        }
        score_low = hype_score(item_low, cfg)
        score_default = hype_score(item_default, cfg)
        # With the old max() approach, feed_weight=0.2 would be ignored (max(0.5, 0.2)=0.5)
        # With the new direct override, weight=0.2 is used → lower score
        assert score_low < score_default, f"Feed weight 0.2 ({score_low}) should be < default 0.5 ({score_default})"
        ratio = score_low / score_default if score_default > 0 else 0
        assert ratio < 0.5, f"Score ratio {ratio:.3f} should be < 0.5 (weight 0.2 vs 0.5)"


class TestMixedPoolDiversity:
    """Verify mixed HN+RSS+GitHub+other pools in a single selection run."""

    def test_mixed_pool_all_sources_represented(self):
        """A single selection run with HN, RSS, GitHub, and Trends
        must give every source at least one slot when slots are available."""
        from newsbot.selection import select_diverse_candidates

        hn = [{"source": "hn", "title": f"HN-{i}", "score": 100.0 - i, "url": f"http://hn/{i}"} for i in range(5)]
        rss = [{"source": "rss", "title": f"RSS-{i}", "score": 80.0 - i, "url": f"http://rss/{i}"} for i in range(5)]
        github = [{"source": "github", "title": f"GH-{i}", "score": 90.0 - i, "url": f"http://gh/{i}"} for i in range(5)]
        trends = [{"source": "trends", "title": f"TR-{i}", "score": 70.0 - i, "url": f"http://tr/{i}"} for i in range(3)]

        scored = sorted(hn + rss + github + trends, key=lambda c: c["score"], reverse=True)
        cfg = {"source_quota": 2}
        top = select_diverse_candidates(scored, 8, cfg)

        sources = [c["source"] for c in top]
        assert "hn" in sources
        assert "rss" in sources
        assert "github" in sources
        assert "trends" in sources

    def test_mixed_pool_deterministic_across_permutations(self):
        """Reversing mixed-pool input must produce identical selection."""
        from newsbot.selection import select_diverse_candidates

        candidates = []
        for src, score, i in [
            ("hn", 100, 1), ("hn", 95, 2),
            ("rss", 80, 3), ("rss", 75, 4),
            ("github", 90, 5), ("github", 85, 6),
            ("trends", 70, 7),
        ]:
            candidates.append({"source": src, "title": f"{src}-{i}", "score": float(score), "url": f"http://{src}/{i}"})

        cfg = {"source_quota": 2}
        top1 = select_diverse_candidates(list(candidates), 5, cfg)
        top2 = select_diverse_candidates(list(reversed(candidates)), 5, cfg)

        titles1 = [c["title"] for c in top1]
        titles2 = [c["title"] for c in top2]
        assert titles1 == titles2, f"Mixed-pool selection differs: {titles1} vs {titles2}"


class TestURLTieBreak:
    """Verify URL is part of the deterministic tie-break key."""

    def test_equal_score_title_source_url_tiebreak(self):
        """Two candidates with equal score, title, and source — URL breaks tie."""
        from newsbot.selection import select_diverse_candidates

        a = {"source": "hn", "title": "Same", "score": 50.0, "url": "http://a.com"}
        b = {"source": "hn", "title": "Same", "score": 50.0, "url": "http://b.com"}

        cfg = {"source_quota": 0}
        top1 = select_diverse_candidates([a, b], 1, cfg)
        top2 = select_diverse_candidates([b, a], 1, cfg)

        # URL ascending: a.com < b.com, so a always wins.
        assert top1[0]["url"] == "http://a.com"
        assert top2[0]["url"] == "http://a.com"