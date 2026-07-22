"""Tests for newsbot/scoring.py — hype_score, recency_decay, topic_bonus."""

from datetime import datetime, timedelta, timezone

from newsbot.config import DEFAULT_TOPIC_BOOST, DEFAULT_SOURCE_WEIGHTS
from newsbot.scoring import engagement, hype_score, recency_decay, score_all, topic_bonus


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
    # Should match both 'llm' and 'local_llm' keywords.
    assert bonus >= DEFAULT_TOPIC_BOOST["llm"] + DEFAULT_TOPIC_BOOST["local_llm"]


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
    # crosspost bonus is +30; topic bonus for 'llm'+'local_llm' is +45.
    assert score > 30 + 45  # engagement contribution must be positive
    assert score > 75.0


def test_hype_score_source_weight_applies():
    # Same engagement on HN (1.2) vs Product Hunt (0.8) → HN should score higher
    # (ignoring topic bonus, which is title-driven; use a neutral title).
    hn = _item(source="hackernews", title="zzz neutral", upvotes=100, comments=10)
    ph = _item(source="producthunt", title="zzz neutral", upvotes=100, comments=10)
    assert hype_score(hn, CFG) > hype_score(ph, CFG)


def test_score_all_stamps_score_on_every_item():
    items = [_item(upvotes=10), _item(upvotes=1000)]
    out = score_all(items, CFG)
    assert all("score" in i and isinstance(i["score"], float) for i in out)
    assert out[1]["score"] > out[0]["score"]