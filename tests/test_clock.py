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


def test_catchup_five_slots():
    """H-6: catch-up logic handles 5 slots (5,9,13,17,21), not just 2."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from newsbot.clock import gen_slots, latest_due_gen_slot

    hours = gen_slots("5,9,13,17,21")
    assert hours == [5, 9, 13, 17, 21]
    tz = ZoneInfo("Asia/Bangkok")

    # 14:30 → most recent due slot is 13:00
    dt = datetime(2026, 8, 16, 14, 30, tzinfo=tz)
    assert latest_due_gen_slot(dt, hours) == "2026-08-16T13"

    # 08:00 → before 09:00, rolls back to 05:00
    assert latest_due_gen_slot(dt.replace(hour=8), hours) == "2026-08-16T05"

    # 03:00 → before first slot (05:00), rolls to previous day's last slot (21:00)
    assert latest_due_gen_slot(dt.replace(hour=3), hours) == "2026-08-15T21"

    # 21:30 → exactly at/after 21:00
    assert latest_due_gen_slot(dt.replace(hour=21, minute=30), hours) == "2026-08-16T21"

    # 22:00 → after last slot, stays at 21:00 today
    assert latest_due_gen_slot(dt.replace(hour=22), hours) == "2026-08-16T21"
