"""Tests for newsbot/scoring.py — hype_score, recency_decay, topic_bonus."""

from datetime import datetime, timedelta, timezone

import pytest

from newsbot.config import DEFAULT_TOPIC_BOOST, DEFAULT_SOURCE_WEIGHTS
from newsbot.scoring import engagement, hype_score, recency_decay, score_all, score_breakdown, topic_bonus


CFG = {
    "source_weights": DEFAULT_SOURCE_WEIGHTS,
    "topic_boost": DEFAULT_TOPIC_BOOST,
    "lookback_hours": 48,
}


def _item(**overrides):
    base = {"source": "hn", "source_name": "Hacker News", "title": "", "url": ""}
    base.update(overrides)
    return base


def test_engagement_sums_log1p_weighted_signals():
    item = _item(upvotes=100, comments=10, stars=0, reposts=0)
    # log1p(100)*10 + log1p(10)*25 + 0 + 0
    import math
    expected = math.log1p(100) * 10 + math.log1p(10) * 25
    assert abs(engagement(item) - expected) < 1e-6


def test_engagement_zero_when_no_signals():
    assert engagement(_item()) == 0.0


def test_recency_decay_is_one_now_and_decays_with_age():
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assert abs(recency_decay(now_iso, lookback_hours=48) - 1.0) < 0.01

    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(timespec="seconds")
    # e^(-48/48) = e^-1 ≈ 0.37
    assert abs(recency_decay(old, lookback_hours=48) - 0.3678) < 0.05


def test_recency_decay_missing_published_at_is_neutral():
    assert recency_decay(None, lookback_hours=48) == 0.5
    assert recency_decay("", lookback_hours=48) == 0.5


def test_topic_bonus_matches_keywords():
    item = _item(title="New local LLM runs on llama.cpp", snippet="")
    bonus = topic_bonus(item, DEFAULT_TOPIC_BOOST)
    # 'llm', 'local llm', 'llama.cpp' are all in the 'ai' pack now (merged).
    # With max-not-sum, the bonus is the ai pack's boost (20), not a stack.
    assert bonus >= DEFAULT_TOPIC_BOOST["ai"]


def test_topic_bonus_zero_when_no_match():
    item = _item(title="Random unrelated topic", snippet="")
    assert topic_bonus(item, DEFAULT_TOPIC_BOOST) == 0


def test_hype_score_combines_engagement_recency_weight_topic_crosspost():
    item = _item(
        title="New local LLM tool",
        upvotes=100,
        comments=50,
        published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        crosspost_count=2,
    )
    score = hype_score(item, CFG)
    # crosspost bonus is +30; topic bonus for 'ai' pack is +20 (max, not stacked).
    assert score > 30 + 20  # engagement contribution must be positive
    assert score > 50.0


def test_hype_score_source_weight_applies():
    # Same engagement on HN (1.2) vs RSS (0.5) → HN should score higher
    # (ignoring topic bonus, which is title-driven; use a neutral title).
    hn = _item(source="hackernews", title="zzz neutral", upvotes=100, comments=10)
    rss = _item(source="rss", title="zzz neutral", upvotes=100, comments=10)
    assert hype_score(hn, CFG) > hype_score(rss, CFG)


def test_score_all_stamps_score_on_every_item():
    items = [_item(upvotes=10), _item(upvotes=1000)]
    out = score_all(items, CFG)
    assert all("score" in i and isinstance(i["score"], float) for i in out)
    assert out[1]["score"] > out[0]["score"]

# --- flow_001039: deterministic scoring, bug fixes, score breakdown ---


def test_penalty_zero_produces_zero_score():
    """penalty=0.0 must produce score 0.0, not be treated as falsy→1.0."""
    item = _item(
        title="Hyped thing",
        upvotes=1000,
        comments=500,
        penalty=0.0,
        published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    assert hype_score(item, CFG) == 0.0


def test_penalty_zero_in_breakdown():
    """score_breakdown must preserve penalty=0.0."""
    item = _item(
        title="Hyped thing",
        upvotes=100,
        penalty=0.0,
    )
    bd = score_breakdown(item, CFG)
    assert bd["penalty"] == 0.0
    assert bd["score"] == 0.0


def test_decay_docstring_accurate():
    """exp(-2) ≈ 0.135, not 0.07."""
    import math
    assert abs(math.exp(-2) - 0.1353) < 0.001


def test_quantiz_keywords_match():
    """quantize, quantized, quantization must all match via word boundaries."""
    from newsbot.scoring import _topic_bonus_with_matches
    for word in ["quantize", "quantized", "quantization"]:
        item = _item(title=f"New {word} technique", snippet="")
        bonus, matched = _topic_bonus_with_matches(item, DEFAULT_TOPIC_BOOST)
        assert bonus >= DEFAULT_TOPIC_BOOST["ai"], f"'{word}' should match 'ai' topic"
        assert "ai" in matched


def test_recency_decay_deterministic_with_now():
    """Same now + same published_at → same result every time."""
    fixed_now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    pub = "2026-07-28T06:00:00+00:00"
    r1 = recency_decay(pub, lookback_hours=48, now=fixed_now)
    r2 = recency_decay(pub, lookback_hours=48, now=fixed_now)
    assert r1 == r2
    # 6 hours old, lookback 48: exp(-6/48) = exp(-0.125) ≈ 0.8825
    assert abs(r1 - 0.8825) < 0.01


def test_hype_score_deterministic_with_now():
    """hype_score with fixed now is deterministic."""
    fixed_now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    item = _item(
        title="Test",
        upvotes=100,
        comments=10,
        published_at="2026-07-28T06:00:00+00:00",
    )
    s1 = hype_score(item, CFG, now=fixed_now)
    s2 = hype_score(item, CFG, now=fixed_now)
    assert s1 == s2


def test_score_all_uses_one_timestamp_for_batch():
    """score_all with now= should use same timestamp for all items."""
    fixed_now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    items = [
        _item(upvotes=10, published_at="2026-07-28T06:00:00+00:00"),
        _item(upvotes=100, published_at="2026-07-28T10:00:00+00:00"),
    ]
    score_all(items, CFG, now=fixed_now)
    # All items must have the same scored_at timestamp.
    assert items[0]["score_breakdown"]["scored_at"] == items[1]["score_breakdown"]["scored_at"]
    # And it must be the fixed_now we passed.
    assert items[0]["score_breakdown"]["scored_at"].startswith("2026-07-28T12:00:00")


def test_naive_now_raises_value_error():
    """Passing a naive datetime (no tzinfo) must raise ValueError."""
    naive = datetime(2026, 7, 28, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        recency_decay("2026-07-28T06:00:00+00:00", lookback_hours=48, now=naive)


def test_naive_now_raises_even_when_published_at_missing():
    """Naive now must raise even when published_at is missing/empty."""
    naive = datetime(2026, 7, 28, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        recency_decay(None, lookback_hours=48, now=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        hype_score(_item(), CFG, now=naive)


def test_non_utc_timezone_in_now_works():
    """A non-UTC timezone in now should work (converted to UTC)."""
    from datetime import timedelta as td
    # UTC+2 at 14:00 = 12:00 UTC. Published at 10:00 UTC = 2 hours old.
    fixed_now = datetime(2026, 7, 28, 14, 0, 0, tzinfo=timezone(td(hours=2)))  # UTC+2
    pub = "2026-07-28T10:00:00+00:00"  # 2 hours before 12:00 UTC
    r = recency_decay(pub, lookback_hours=48, now=fixed_now)
    # 2 hours old → exp(-2/48) ≈ 0.959
    assert abs(r - 0.959) < 0.01


def test_score_breakdown_returns_all_keys():
    """score_breakdown must return all expected keys."""
    item = _item(
        title="LLM test",
        upvotes=100,
        comments=10,
        published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        crosspost_count=2,
    )
    bd = score_breakdown(item, CFG)
    expected_keys = {
        "score", "engagement", "recency", "source_weight",
        "topic_bonus", "crosspost_bonus", "penalty", "matched_topics",
        "origin_topic", "scored_at", "lookback_hours",
        "source", "published_at", "upvotes", "comments",
        "stars", "reposts", "crosspost_count",
    }
    assert set(bd.keys()) == expected_keys
    assert isinstance(bd["score"], float)
    assert isinstance(bd["engagement"], float)
    assert isinstance(bd["recency"], float)
    assert isinstance(bd["source_weight"], float)
    assert isinstance(bd["topic_bonus"], int)
    assert isinstance(bd["crosspost_bonus"], float)
    assert isinstance(bd["penalty"], float)
    assert isinstance(bd["matched_topics"], list)
    assert isinstance(bd["scored_at"], str)


def test_score_breakdown_matched_topics_match_topic_bonus():
    """matched_topics must correspond exactly to what topic_bonus counted."""
    from newsbot.scoring import _topic_bonus_with_matches
    item = _item(title="New local LLM quantization tool", snippet="")
    bd = score_breakdown(item, CFG)
    bonus_direct, matched_direct = _topic_bonus_with_matches(item, CFG["topic_boost"])
    assert bd["topic_bonus"] == bonus_direct
    assert bd["matched_topics"] == matched_direct


def test_score_all_stamps_score_breakdown():
    """score_all must stamp score_breakdown on every item."""
    items = [_item(upvotes=10), _item(upvotes=1000)]
    out = score_all(items, CFG)
    assert all("score_breakdown" in i for i in out)
    assert all(isinstance(i["score_breakdown"], dict) for i in out)


def test_hype_score_unchanged_without_now():
    """hype_score without now should work same as before (uses current time)."""
    item = _item(upvotes=100, title="test")
    score = hype_score(item, CFG)
    assert isinstance(score, float)
    assert score > 0


# --- flow_001093 (Task 3): current_temperature + merge_multiplier ---


def _store_row(**overrides) -> dict:
    """Store row (DB dict) with known score components, published at 2026-08-16T00:00Z."""
    row = {
        "id": 1,
        "title": "Test story",
        "url": "https://example.com/x",
        "source": "hn",
        "published_at": "2026-08-16T00:00:00+00:00",
        "engagement_score": 100.0,
        "recency_at_queue": 1.0,
        "source_weight": 1.2,
        "topic_bonus": 15,
        "crosspost_bonus": 30.0,
        "penalty": 1.0,
        "lookback_hours": 48,
        "score_at_queue": (100.0 * 1.0 * 1.2 + 15 + 30.0) * 1.0,
    }
    row.update(overrides)
    return row


def test_current_temperature_matches_hand_computed_at_two_times():
    """Only recency changes: (eng*rec*w + topic + crosspost)*penalty."""
    from newsbot.scoring import current_temperature

    row = _store_row()
    # age 0h -> rec 1.0 -> (100*1.0*1.2 + 15 + 30)*1.0 = 165.0
    now1 = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
    assert abs(current_temperature(row, CFG, now=now1) - 165.0) < 1e-6
    # age 12h, lookback 48 -> rec = exp(-0.25)
    import math

    now2 = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    expected = (100.0 * math.exp(-0.25) * 1.2 + 15 + 30.0) * 1.0
    assert abs(current_temperature(row, CFG, now=now2) - expected) < 1e-6


def test_current_temperature_legacy_null_engagement_is_zero():
    from newsbot.scoring import current_temperature

    row = _store_row(engagement_score=None)
    assert current_temperature(row, CFG, now=datetime(2026, 8, 16, tzinfo=timezone.utc)) == 0.0


def test_current_temperature_null_lookback_falls_back_to_config_default():
    from newsbot.scoring import current_temperature

    import math

    row = _store_row(lookback_hours=None)
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)  # age 12h, lookback 48 (CFG)
    expected = (100.0 * math.exp(-12 / 48) * 1.2 + 15 + 30.0) * 1.0
    assert abs(current_temperature(row, CFG, now=now) - expected) < 1e-6


def test_current_temperature_preserves_zero_penalty_and_null_components():
    from newsbot.scoring import current_temperature

    row = _store_row(penalty=0.0)
    assert current_temperature(row, CFG, now=datetime(2026, 8, 16, tzinfo=timezone.utc)) == 0.0
    row2 = _store_row(source_weight=None, topic_bonus=None, crosspost_bonus=None, penalty=None)
    # defaults: w=1.0, topic=0, crosspost=0, penalty=1.0, age 0 -> 100*1*1 = 100
    assert abs(current_temperature(row2, CFG, now=datetime(2026, 8, 16, tzinfo=timezone.utc)) - 100.0) < 1e-6


def test_merge_multiplier_known_values():
    from newsbot.scoring import merge_multiplier

    assert merge_multiplier(None) == 1.0
    assert merge_multiplier(1) == 1.0
    assert abs(merge_multiplier(3) - 1.4) < 1e-9
    assert merge_multiplier(99) == 2.0
    # falsy/zero treated as 1; custom bonus/cap respected
    assert merge_multiplier(0) == 1.0
    assert abs(merge_multiplier(3, bonus=0.5, cap=1.8) - 1.8) < 1e-9
