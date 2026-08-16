def test_local_now_default_bangkok(monkeypatch):
    monkeypatch.delenv("NEWS_TZ", raising=False)
    from newsbot.clock import local_now
    assert str(local_now().tzinfo) == "Asia/Bangkok"


def test_local_now_env_override(monkeypatch):
    monkeypatch.setenv("NEWS_TZ", "Asia/Makassar")
    from newsbot.clock import local_now
    assert str(local_now().tzinfo) == "Asia/Makassar"


def test_slot_keys():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from newsbot.clock import gen_slots, post_slot, summary_day, latest_due_gen_slot
    dt = datetime(2026, 8, 16, 14, 30, tzinfo=ZoneInfo("Asia/Bangkok"))
    assert post_slot(dt) == "2026-08-16T14"
    assert post_slot(dt.replace(hour=13)) is None          # odd hour
    assert summary_day(dt) == "2026-08-16"
    assert gen_slots("5,17") == [5, 17]
    # catch-up: most recent scheduled gen slot at or before 14:30 is 05:00 today
    assert latest_due_gen_slot(dt, [5, 17]) == "2026-08-16T05"
    assert latest_due_gen_slot(dt.replace(hour=18), [5, 17]) == "2026-08-16T17"
    assert latest_due_gen_slot(dt.replace(hour=3), [5, 17]) == "2026-08-15T17"
