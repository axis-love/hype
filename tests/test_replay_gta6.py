"""Acceptance test: GTA6 leak story ranks #1 in the replay fixture.

This test encodes the plan's acceptance criteria for the score replay (H-5):
  1. The GTA6 leak story ranks #1 in the replay.
  2. It clears the pick threshold (min_score).
  3. >=3 distinct origin topics appear in the top 14.

The fixture (tests/fixtures/gta6_week.json) holds a captured week of real
candidates (Aug 17-21, 2026) fetched with the new collectors: Reddit gaming
+ AI + hardware + science subs with real score/comment counts, IGN,
Eurogamer, HN, and a reconstructed Google Trends "GTA 6 leak" entry with
Breakout traffic.

Also tests that store mode (no-arg default) does not crash: constructs a
temp DB with a pending_posts row via the db module, runs _load_store_candidates.

If this test regresses, the tuning has drifted — investigate before adjusting.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.settings_store import SettingsStore, SettingsStoreConfig
from newsbot.collectors.base import Candidate
from newsbot.config import load_config
from newsbot.db import NewsStore
from newsbot.dedupe import dedupe_and_merge
from newsbot.scoring import score_all

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gta6_week.json"


def _load_fixture() -> list[dict]:
    """Load the GTA6 week fixture as a list of candidate dicts."""
    with open(FIXTURE_PATH) as f:
        return json.load(f)


def _replay(candidates: list[dict], config: dict) -> list[dict]:
    """Run dedupe_and_merge + score_all, return ranked list (highest first)."""
    # Use a fixed 'now' for deterministic scoring — the fixture's timestamps
    # are from Aug 22-23, 2026, so we score as of Aug 23 noon UTC.
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)
    as_candidates: list = [Candidate.from_dict(c) for c in candidates]
    merged = dedupe_and_merge(as_candidates)
    scored = score_all([c.to_dict() for c in merged], config, now=now)
    scored.sort(key=lambda c: c.get("score", 0.0), reverse=True)
    return scored


@pytest.fixture
def config():
    """Load the active config from the settings store (defaults if no DB)."""
    db_path = Path(__file__).resolve().parents[1] / "data" / "newsbot.sqlite"
    store = SettingsStore(SettingsStoreConfig(db_path=db_path))
    return load_config(store)


@pytest.fixture
def scored_fixture(config):
    """Load and score the fixture under the current config."""
    candidates = _load_fixture()
    return _replay(candidates, config)


def test_gta6_leak_ranks_number_one(scored_fixture):
    """The GTA6 leak story must rank #1 in the replay.

    The GTA6 leak is represented by a reconstructed Google Trends entry
    with Breakout traffic (reposts=1000000) that merges with the matching
    IGN/Eurogamer articles via the trends containment rule. The crosspost
    bonus (+30) and the massive reposts engagement make it the top story.
    """
    top = scored_fixture[0]
    title = str(top.get("title") or "").lower()
    # The #1 item must be the GTA6 leak story — either the trends-merged
    # article or a Reddit post about the GTA6 leak.
    assert "gta" in title, (
        f"Expected GTA6 story at #1, got: {top.get('title')!r} "
        f"(score={top.get('score'):.2f})"
    )


def test_gta6_leak_clears_threshold(scored_fixture, config):
    """The #1 story must clear the pick threshold (min_score)."""
    min_score = float(config.get("min_score") or 35)
    top = scored_fixture[0]
    score = top.get("score", 0.0)
    assert score >= min_score, (
        f"Top story score {score:.2f} below threshold {min_score:.1f}"
    )


def test_at_least_three_distinct_topics_in_top_14(scored_fixture):
    """At least 3 distinct origin topics must appear in the top 14.

    This ensures the topic diversity goal: the digest should surface stories
    from multiple topics (gaming, ai, science, hardware, ...), not just
    one. The origin_topic is derived from the candidate's source_name via
    the pack table (e.g. r/gaming → gaming, r/science → science).
    """
    top14 = scored_fixture[:14]
    topics: set[str] = set()
    for item in top14:
        bd = item.get("score_breakdown") or {}
        ot = bd.get("origin_topic")
        if ot:
            topics.add(ot)
    assert len(topics) >= 3, (
        f"Expected >=3 distinct origin topics in top 14, got {len(topics)}: "
        f"{sorted(topics)}"
    )


def test_gta6_leak_has_full_score_breakdown(scored_fixture):
    """The #1 story must carry a full score breakdown for auditability.

    The breakdown includes: engagement, recency, source_weight, topic_bonus,
    crosspost_bonus, origin_topic, matched_topics, and the pick threshold.
    """
    top = scored_fixture[0]
    bd = top.get("score_breakdown") or {}
    required = {
        "engagement", "recency", "source_weight", "topic_bonus",
        "crosspost_bonus", "origin_topic", "matched_topics",
        "crosspost_count", "score",
    }
    missing = required - set(bd.keys())
    assert not missing, f"Missing breakdown keys: {missing}"
    # The GTA6 story should have a crosspost bonus (merged from trends + RSS).
    assert bd["crosspost_bonus"] > 0, "GTA6 story should have crosspost bonus"
    assert bd["origin_topic"] == "gaming", (
        f"Expected origin_topic=gaming, got {bd.get('origin_topic')!r}"
    )


# --- Store mode tests (review fix: no-arg default = score the current store) ---


def _make_store_with_row(tmpdir: str) -> str:
    """Create a temp SQLite DB with one pending_posts row via NewsStore.

    Returns the path to the temp DB. Uses the db module's own
    add_stories_to_store() so the row schema is exactly what prod
    writes — no hand-rolled SQL.
    """
    db_path = str(Path(tmpdir) / "test.sqlite")
    store = NewsStore(Path(db_path))
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc).isoformat(timespec="seconds")
    story = {
        "title": "GTA 6 gameplay leaks online ahead of showcase",
        "url": "https://www.ign.com/articles/gta-6-leak",
        "source": "reddit",
        "source_name": "r/gaming",
        "snippet": "Gameplay footage leaked",
        "published_at": now,
        "upvotes": 5000,
        "comments": 300,
        "score": 200.0,
        "score_breakdown": {
            "score": 200.0,
            "engagement": 150.0,
            "recency": 1.0,
            "source_weight": 1.0,
            "topic_bonus": 20,
            "crosspost_bonus": 30.0,
            "penalty": 1.0,
            "matched_topics": ["gaming"],
            "origin_topic": "gaming",
            "crosspost_count": 2,
            "upvotes": 5000,
            "comments": 300,
            "stars": 0,
            "reposts": 0,
            "lookback_hours": 48,
            "source": "reddit",
            "published_at": now,
            "scored_at": now,
        },
    }
    store.add_stories_to_store([story], [])
    store.close()
    return db_path


def test_store_mode_does_not_crash():
    """_load_store_candidates must load rows from the DB without crashing.

    Regression test for the review bug where store mode used hand-rolled
    SQL against a 'store' table that didn't exist (the real table is
    'pending_posts'). Now uses NewsStore.list_store_rows() — the same
    accessor the poster uses.
    """
    # Import the replay module's store loader.
    import sys
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from replay_scores import _load_store_candidates

    tmpdir = tempfile.mkdtemp()
    try:
        db_path = _make_store_with_row(tmpdir)
        candidates = _load_store_candidates(db_path)
        assert len(candidates) == 1, f"Expected 1 candidate, got {len(candidates)}"
        c = candidates[0]
        assert c["title"] == "GTA 6 gameplay leaks online ahead of showcase"
        assert c["source"] == "reddit"
        assert c["source_name"] == "r/gaming"
        assert c["upvotes"] == 5000
        assert c["comments"] == 300
    finally:
        shutil.rmtree(tmpdir)
