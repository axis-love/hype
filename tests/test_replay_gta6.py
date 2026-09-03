"""Acceptance test: GTA6 leak story ranks #1 in the replay fixture.

This test encodes the plan's acceptance criteria for the score replay (H-5/H-9):
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
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.settings_store import SettingsStore, SettingsStoreConfig
from newsbot.config import load_config
from newsbot.db import NewsStore

# Import replay from the script — the code under test is the actual tool.
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from replay_scores import (  # noqa: E402
    _fixture_now,
    _load_store_candidates,
    replay,
    replay_with_selection,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gta6_week.json"


def _load_fixture() -> list[dict]:
    """Load the GTA6 week fixture as a list of candidate dicts."""
    with open(FIXTURE_PATH) as f:
        return json.load(f)


# --- Module-scope fixtures (parse the 6049-line JSON once, not 4×) ---


@pytest.fixture(scope="module")
def config() -> dict:
    """Load the active config from the settings store (defaults if no DB)."""
    db_path = Path(__file__).resolve().parents[1] / "data" / "newsbot.sqlite"
    store = SettingsStore(SettingsStoreConfig(db_path=db_path))
    return load_config(store)


@pytest.fixture(scope="module")
def fixture_candidates() -> list[dict]:
    """Load the fixture once for all tests in this module."""
    return _load_fixture()


@pytest.fixture(scope="module")
def scored_fixture(config, fixture_candidates) -> list[dict]:
    """Score the fixture under the current config using replay()."""
    # Use the fixture's max published_at as 'now' — same default as the CLI.
    now = _fixture_now(fixture_candidates)
    return replay(fixture_candidates, config, now=now)


# --- Acceptance tests (H-5 criteria, now using replay() from the script) ---


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


# --- H-9 replay fidelity tests ---


def test_replay_honors_pre_merge_weights(config, fixture_candidates):
    """replay() must call _set_pre_merge_weights before dedupe.

    Without it, --weights overrides don't affect primary-source selection
    in dedupe. We verify by checking that the dedupe module's
    _PRE_MERGE_WEIGHTS dict matches config["source_weights"] after replay.
    """
    from newsbot.dedupe import _PRE_MERGE_WEIGHTS

    now = _fixture_now(fixture_candidates)
    replay(fixture_candidates, config, now=now)

    # After replay, _PRE_MERGE_WEIGHTS should match config's source_weights.
    cfg_weights = config.get("source_weights") or {}
    for src, w in cfg_weights.items():
        actual = _PRE_MERGE_WEIGHTS.get(src, 1.0)
        assert abs(actual - w) < 0.01, (
            f"Pre-merge weight for {src!r}: expected {w}, got {actual}"
        )


def test_replay_runs_selection_stage(config, fixture_candidates):
    """replay_with_selection() must return both raw ranking and selected set.

    The selected set uses select_diverse_candidates (source quota, round-robin)
    — the same function main.py uses. The selected set should be <= the
    raw ranking in length and should contain items from >=2 sources.
    """
    now = _fixture_now(fixture_candidates)
    scored, selected = replay_with_selection(
        fixture_candidates, config, now=now
    )
    assert scored, "Raw ranking should not be empty"
    assert selected, "Selected set should not be empty"
    assert len(selected) <= len(scored), (
        f"Selected ({len(selected)}) should be <= raw ({len(scored)})"
    )
    # Selected set should have items from at least 2 distinct sources
    # (round-robin allocation guarantees source diversity).
    sources = {c.get("source") for c in selected}
    assert len(sources) >= 2, (
        f"Expected >=2 distinct sources in selected set, got {len(sources)}: {sources}"
    )


def test_fixture_now_defaults_to_max_published_at(fixture_candidates):
    """_fixture_now() must return the max published_at in the fixture.

    Wall-clock today makes frozen Aug-2026 fixtures decay to ~0 recency.
    The fixture-now default keeps scores pinned to the capture window.
    """
    now = _fixture_now(fixture_candidates)
    # The fixture's timestamps are from Aug 2026 — verify now is in that range.
    assert now.year == 2026, f"Expected 2026, got {now.year}"
    assert now.month == 8, f"Expected August, got month {now.month}"

    # Verify it matches the actual max published_at.
    max_ts = max(
        str(c.get("published_at") or "")
        for c in fixture_candidates
        if c.get("published_at")
    )
    expected = datetime.fromisoformat(max_ts.replace("Z", "+00:00"))
    assert now == expected, (
        f"Expected fixture now = {expected}, got {now}"
    )


def test_store_mode_fails_on_missing_db(tmp_path):
    """_load_store_candidates must fail on a missing DB, not silently create one.

    Silently creating an empty store masks misconfiguration — the operator
    thinks they're replaying the store but get zero candidates.
    """
    missing_db = str(tmp_path / "nonexistent.sqlite")
    with pytest.raises(FileNotFoundError, match="Store DB not found"):
        _load_store_candidates(missing_db)


def test_store_mode_tolerates_legacy_source_ids(tmp_path):
    """_load_store_candidates must tolerate legacy source ids in migrated DBs.

    A DB migrated from the Product Hunt era may have rows with source=
    'producthunt'. The old Candidate.from_dict would raise ValueError;
    the new code skips the Candidate roundtrip entirely and works with
    plain dicts, so legacy source ids pass through without error.
    """
    db_path = str(tmp_path / "legacy.sqlite")
    store = NewsStore(Path(db_path))
    now_iso = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc).isoformat(timespec="seconds")
    story = {
        "title": "Some old Product Hunt launch",
        "url": "https://producthunt.com/posts/some-launch",
        "source": "producthunt",
        "source_name": "Product Hunt",
        "snippet": "Legacy entry from migrated DB",
        "published_at": now_iso,
        "upvotes": 100,
        "comments": 10,
        "score": 50.0,
        "score_breakdown": {
            "score": 50.0,
            "engagement": 30.0,
            "recency": 1.0,
            "source_weight": 0.5,
            "topic_bonus": 0,
            "crosspost_bonus": 0.0,
            "penalty": 1.0,
            "matched_topics": [],
            "origin_topic": None,
            "crosspost_count": 1,
            "upvotes": 100,
            "comments": 10,
            "stars": 0,
            "reposts": 0,
            "lookback_hours": 48,
            "source": "producthunt",
            "published_at": now_iso,
            "scored_at": now_iso,
        },
    }
    store.add_stories_to_store([story], [])
    store.close()

    # Must not raise ValueError on the legacy source id.
    candidates = _load_store_candidates(db_path)
    assert len(candidates) == 1
    c = candidates[0]
    assert c["source"] == "producthunt"
    assert c["title"] == "Some old Product Hunt launch"


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
    'pending_posts'). Now uses NewsStore.list_store_rows("telegram") — the same
    accessor the poster uses.
    """
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
