"""schedule.due() is the hourly-workflow gate: pure, no I/O. These tests pin
the boundary rules the workflow relies on -- get one wrong and a fork either
never fires, or fires every hour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mailtriage.config import Config
from mailtriage.schedule import current_slot, due, max_gap_hours


def _cfg(**kw: object) -> Config:
    return Config(delivery="email", **kw)  # type: ignore[arg-type]


# --- max_gap_hours -----------------------------------------------------


def test_max_gap_wrap_around():
    assert max_gap_hours(["08:00", "18:00"]) == 14


def test_max_gap_single_slot_is_full_day():
    assert max_gap_hours(["08:00"]) == 24


def test_max_gap_unsorted_input():
    assert max_gap_hours(["18:00", "08:00"]) == 14


# --- due(): boundary at a slot (UTC, no tz conversion involved) --------


def test_due_exactly_at_slot():
    cfg = _cfg(run_at=["08:00"])
    now = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    assert due(cfg, now) == "digest"


def test_due_59_minutes_after_slot():
    cfg = _cfg(run_at=["08:00"])
    now = datetime(2026, 1, 1, 8, 59, tzinfo=timezone.utc)
    assert due(cfg, now) == "digest"


def test_still_due_90_minutes_after_slot_default_catch_up():
    """GitHub skips cron hours under load (one run in five hours, observed
    2026-09-03): the hour after a skipped one must still fire that slot."""
    cfg = _cfg(run_at=["08:00"])
    now = datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)
    assert due(cfg, now) == "digest"


def test_not_due_120_minutes_after_slot():
    cfg = _cfg(run_at=["08:00"])
    now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    assert due(cfg, now) is None


def test_catch_up_minutes_is_honored():
    cfg = _cfg(run_at=["08:00"], catch_up_minutes=60)
    assert due(cfg, datetime(2026, 1, 1, 8, 59, tzinfo=timezone.utc)) == "digest"
    assert due(cfg, datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)) is None
    wide = _cfg(run_at=["08:00"], catch_up_minutes=360)
    assert due(wide, datetime(2026, 1, 1, 13, 59, tzinfo=timezone.utc)) == "digest"


def test_not_due_one_minute_before_slot():
    cfg = _cfg(run_at=["08:00"])
    now = datetime(2026, 1, 1, 7, 59, tzinfo=timezone.utc)
    assert due(cfg, now) is None


def test_late_night_slot_caught_just_after_local_midnight():
    """A 23:30 slot must still fire when the hourly cron's first tick after it
    lands at, say, 00:10 the next day -- not just the same day at 23:30-59."""
    cfg = _cfg(run_at=["23:30"])
    now = datetime(2026, 1, 2, 0, 10, tzinfo=timezone.utc)
    assert due(cfg, now) == "digest"


def test_late_night_slot_caught_up_across_midnight():
    cfg = _cfg(run_at=["23:30"])
    now = datetime(2026, 1, 2, 1, 15, tzinfo=timezone.utc)  # 105 min later, past midnight
    assert due(cfg, now) == "digest"
    assert current_slot(cfg, now) == datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)


def test_late_night_slot_not_due_far_into_next_day():
    cfg = _cfg(run_at=["23:30"])
    now = datetime(2026, 1, 2, 1, 30, tzinfo=timezone.utc)
    assert due(cfg, now) is None


# --- timezone conversion -------------------------------------------------


def test_timezone_conversion_from_utc_now():
    """A slot at 08:00 America/New_York, evaluated from a UTC `now` -- due()
    must convert, not compare the raw UTC clock to the local slot string."""
    cfg = _cfg(run_at=["08:00"], timezone="America/New_York")
    local = datetime(2026, 1, 15, 8, 0, tzinfo=ZoneInfo("America/New_York"))  # EST, UTC-5
    now_utc = local.astimezone(timezone.utc)
    assert now_utc.hour == 13  # sanity: 08:00 EST really is 13:00 UTC
    assert due(cfg, now_utc) == "digest"

    # An hour earlier in UTC is an hour earlier locally too -- not due yet.
    assert due(cfg, now_utc - timedelta(hours=1)) is None


def test_dst_spring_forward_still_resolves():
    """2026-03-08 is when America/New_York springs forward (2am -> 3am). A
    slot well clear of the transition must still resolve correctly."""
    cfg = _cfg(run_at=["08:00"], timezone="America/New_York")
    local = datetime(2026, 3, 8, 8, 0, tzinfo=ZoneInfo("America/New_York"))  # already EDT, UTC-4
    now_utc = local.astimezone(timezone.utc)
    assert now_utc.hour == 12
    assert due(cfg, now_utc) == "digest"


def test_dst_spring_forward_catch_up_across_the_gap():
    """01:30 slot on the spring-forward night: 02:00-03:00 doesn't exist, so
    the wall clock jumps and a cron at 03:20 local is 50 real minutes late
    (due), while 04:20 local is 110 real minutes late -- still inside 120
    by the wall-clock arithmetic due() uses, and that is what the stamp says."""
    cfg = _cfg(run_at=["01:30"], timezone="America/New_York")
    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 3, 8, 3, 20, tzinfo=tz).astimezone(timezone.utc)
    assert due(cfg, now) == "digest"
    assert current_slot(cfg, now) == datetime(2026, 3, 8, 1, 30, tzinfo=tz)
    assert due(cfg, datetime(2026, 3, 8, 4, 20, tzinfo=tz).astimezone(timezone.utc)) is None


def test_dst_fall_back_still_resolves():
    """2026-11-01 is when America/New_York falls back (2am -> 1am)."""
    cfg = _cfg(run_at=["08:00"], timezone="America/New_York")
    local = datetime(2026, 11, 1, 8, 0, tzinfo=ZoneInfo("America/New_York"))  # already EST, UTC-5
    now_utc = local.astimezone(timezone.utc)
    assert now_utc.hour == 13
    assert due(cfg, now_utc) == "digest"


# --- weekly_review ---------------------------------------------------------


def test_weekly_slot_matches_weekday():
    cfg = _cfg(run_at=["06:00"], weekly_review="wed 09:00")
    now = datetime(2026, 1, 7, 9, 15, tzinfo=timezone.utc)  # a Wednesday
    assert due(cfg, now) == "weekly"


def test_weekly_slot_wrong_weekday_not_due():
    cfg = _cfg(run_at=["06:00"], weekly_review="wed 09:00")
    now = datetime(2026, 1, 2, 9, 15, tzinfo=timezone.utc)  # a Friday
    assert due(cfg, now) is None


def test_weekly_weekday_is_the_slot_day_not_the_catch_up_day():
    """A "sun 23:30" weekly slot caught at 00:40 Monday is still Sunday's."""
    cfg = _cfg(run_at=["06:00"], weekly_review="sun 23:30")
    now = datetime(2026, 1, 5, 0, 40, tzinfo=timezone.utc)  # Monday 00:40
    assert due(cfg, now) == "weekly"
    assert current_slot(cfg, now) == datetime(2026, 1, 4, 23, 30, tzinfo=timezone.utc)


def test_both_due_same_hour_digest_wins():
    cfg = _cfg(run_at=["09:00"], weekly_review="wed 09:00")
    now = datetime(2026, 1, 7, 9, 0, tzinfo=timezone.utc)  # Wednesday, both slots hit
    assert due(cfg, now) == "digest"


# --- workflow_dispatch ------------------------------------------------------


def test_workflow_dispatch_always_digest_regardless_of_time():
    cfg = _cfg(run_at=["08:00"])
    now = datetime(2026, 1, 1, 15, 37, tzinfo=timezone.utc)  # nowhere near the slot
    assert due(cfg, now, event="workflow_dispatch") == "digest"


# --- current_slot: the no-double-send stamp ------------------------------


def test_current_slot_none_when_not_due():
    cfg = _cfg(run_at=["08:00"])
    assert current_slot(cfg, datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)) is None


def test_current_slot_is_local_time():
    cfg = _cfg(run_at=["08:00"], timezone="America/New_York")
    now = datetime(2026, 1, 15, 8, 45, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    slot = current_slot(cfg, now)
    assert slot is not None
    assert (slot.hour, slot.minute) == (8, 0)
    assert f"{slot:%a %d %b %H:%M}" == "Thu 15 Jan 08:00"


def test_current_slot_picks_most_recent_when_two_overlap():
    """run_at an hour apart with a 120-minute window: at 09:17 both 08:00 and
    09:00 match, and the stamp must be 09:00 -- else the guard finds 08:00's
    digest and suppresses this one."""
    cfg = _cfg(run_at=["08:00", "09:00"])
    now = datetime(2026, 1, 1, 9, 17, tzinfo=timezone.utc)
    assert current_slot(cfg, now) == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


def test_current_slot_none_for_workflow_dispatch():
    cfg = _cfg(run_at=["08:00"])
    now = datetime(2026, 1, 1, 8, 5, tzinfo=timezone.utc)
    assert due(cfg, now, event="workflow_dispatch") == "digest"
    assert current_slot(cfg, now, event="workflow_dispatch") is None
