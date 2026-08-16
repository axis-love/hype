"""Tests for newsbot/selection.py — pure temperature-gated pick logic (flow_001093)."""

from datetime import datetime, timezone

from newsbot.selection import PickResult, pick_hottest

CFG = {"lookback_hours": 48}
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)

# Knobs matching the plan's defaults.
FLOOR, RATIO, BONUS, CAP = 35.0, 0.5, 0.2, 2.0


def _row(row_id: int, temp: float, merge_count: int = 1) -> dict:
    """Build a store row whose current_temperature at NOW equals `temp`.

    source_weight 1.0, no topic/crosspost bonus, penalty 1.0, recency 1.0
    (published_at == NOW, so rec = exp(0) = 1). temp == engagement_score.
    """
    return {
        "id": row_id,
        "title": f"story {row_id}",
        "published_at": NOW.isoformat(),
        "engagement_score": temp,
        "source_weight": 1.0,
        "topic_bonus": 0,
        "crosspost_bonus": 0.0,
        "penalty": 1.0,
        "lookback_hours": 48,
        "merge_count": merge_count,
    }


def _pick(rows, **kw) -> PickResult:
    return pick_hottest(
        rows,
        CFG,
        now=NOW,
        floor=kw.pop("floor", FLOOR),
        ratio=kw.pop("ratio", RATIO),
        merge_bonus=kw.pop("merge_bonus", BONUS),
        merge_cap=kw.pop("merge_cap", CAP),
        **kw,
    )


def test_empty_rows_gives_empty():
    result = _pick([])
    assert result.reason == "empty"
    assert result.row is None
    assert result.temps == {}


def test_all_below_floor_gives_below_threshold_with_correct_threshold():
    rows = [_row(1, 10.0), _row(2, 20.0), _row(3, 30.0)]
    result = _pick(rows)
    # threshold = max(floor=35, 0.5 * median([10,20,30])=10) = 35
    assert result.reason == "below_threshold"
    assert result.row is None
    assert result.threshold == 35.0
    assert result.median == 20.0
    assert result.hottest == 30.0
    assert set(result.temps.keys()) == {1, 2, 3}


def test_adaptive_threshold_dominates_when_store_hot():
    rows = [_row(1, 100.0), _row(2, 200.0), _row(3, 300.0)]
    result = _pick(rows)
    # threshold = max(35, 0.5*200) = 100; all eligible; hottest wins.
    assert result.reason == "picked"
    assert result.threshold == 100.0
    assert result.row["id"] == 3


def test_pick_chooses_hottest_by_raw_temp():
    rows = [_row(1, 50.0), _row(2, 90.0), _row(3, 70.0)]
    result = _pick(rows)
    assert result.reason == "picked"
    assert result.row["id"] == 2
    assert result.hottest == 90.0


def test_merge_multiplier_flips_ranking():
    # Row A raw 100, row B raw 80 with 6 merges -> 80 * min(1+0.2*5, 2) = 80*2.0 = 160.
    rows = [_row(1, 100.0, merge_count=1), _row(2, 80.0, merge_count=6)]
    result = _pick(rows)
    assert result.reason == "picked"
    assert result.row["id"] == 2


def test_merge_multiplier_never_grants_eligibility():
    # Row A raw 100 (eligible: threshold = max(35, 0.5*median([100, 10])) = 35...
    # actually threshold = max(35, 0.5*55.0)=35 -> 100 eligible, 10 not).
    rows = [_row(1, 100.0), _row(2, 10.0, merge_count=99)]
    result = _pick(rows)
    assert result.reason == "picked"
    # Ineligible row (10 < 35) must never win despite 99 merges (mult capped 2.0).
    assert result.row["id"] == 1


def test_below_threshold_still_populates_stats():
    rows = [_row(1, 10.0), _row(2, 20.0)]
    result = _pick(rows)
    assert result.reason == "below_threshold"
    assert result.temps == {1: 10.0, 2: 20.0}
    assert result.hottest == 20.0
    assert result.median == 15.0
    assert result.threshold == 35.0


def test_temps_dict_populated_for_all_rows():
    rows = [_row(1, 50.0), _row(2, 90.0), _row(3, 70.0)]
    result = _pick(rows)
    assert result.temps == {1: 50.0, 2: 90.0, 3: 70.0}


def test_legacy_null_score_rows_get_zero_temp_and_never_win():
    rows = [_row(1, 0.0)]
    rows[0]["engagement_score"] = None  # legacy row, queued before scoring update
    result = _pick(rows)
    # 0.0 temp < floor 35 -> below_threshold, not picked.
    assert result.reason == "below_threshold"
    assert result.temps == {1: 0.0}


def test_eligible_row_exactly_at_threshold_is_picked():
    # threshold = max(35, 0.5 * 70) = 35; row at exactly 35 must be eligible (>=).
    rows = [_row(1, 35.0), _row(2, 70.0)]
    result = _pick(rows)
    assert result.reason == "picked"
    assert result.threshold == 35.0
