"""schedule.due() is the hourly-workflow gate: pure, no I/O. These tests pin
the boundary rules the workflow relies on -- get one wrong and a fork either
never fires, or fires every hour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mailtriage.config import Config
from mailtriage.schedule import due, max_gap_hours


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


def test_not_due_60_minutes_after_slot():
    cfg = _cfg(run_at=["08:00"])
    now = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert due(cfg, now) is None


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


def test_both_due_same_hour_digest_wins():
    cfg = _cfg(run_at=["09:00"], weekly_review="wed 09:00")
    now = datetime(2026, 1, 7, 9, 0, tzinfo=timezone.utc)  # Wednesday, both slots hit
    assert due(cfg, now) == "digest"


# --- workflow_dispatch ------------------------------------------------------


def test_workflow_dispatch_always_digest_regardless_of_time():
    cfg = _cfg(run_at=["08:00"])
    now = datetime(2026, 1, 1, 15, 37, tzinfo=timezone.utc)  # nowhere near the slot
    assert due(cfg, now, event="workflow_dispatch") == "digest"
