""" "Is it time?" -- the gate between the hourly workflow trigger and a real run.

Users pick their own daily times (`run_at`) and an optional weekly slot
(`weekly_review`) in their own timezone. Rewriting .github/workflows/digest.yml
per-fork from the wizard would force every user to grant it the `workflow` PAT
scope just to change their schedule, so instead the workflow runs HOURLY and
this module -- pure, no I/O -- decides whether the current hour is a slot.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from itertools import pairwise
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from mailtriage.config import Config

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def local_zone(name: str) -> ZoneInfo | dt_timezone:
    """ZoneInfo(name), with one tiny fallback: some Windows Pythons have no
    tzdata package installed, so even the built-in "UTC" name fails to
    resolve. Only "UTC" gets the fallback -- any other bad name still raises,
    since it can't be guessed."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "UTC":
            return dt_timezone.utc
        raise


def max_gap_pair(run_at: list[str]) -> tuple[float, str, str]:
    """The largest gap between consecutive daily slots, wrapping past
    midnight, as (hours, slot_before, slot_after). A single slot's own gap is
    the full day (it "recurs" every 24h)."""
    slots = sorted(set(run_at), key=_minutes)
    if len(slots) == 1:
        return 24.0, slots[0], slots[0]
    ring = list(pairwise(slots)) + [(slots[-1], slots[0])]
    gaps = [(((_minutes(b) - _minutes(a)) % (24 * 60)) / 60.0, a, b) for a, b in ring]
    return max(gaps, key=lambda t: t[0])


def max_gap_hours(run_at: list[str]) -> float:
    return max_gap_pair(run_at)[0]


def _slot_start(now_local: datetime, slot: str, catch_up_minutes: int) -> datetime | None:
    """The slot's local datetime when `now_local` is within `catch_up_minutes`
    after it, else None. GitHub's hourly cron fires 5-30 min late and under
    load skips hours outright (one run in five hours, observed 2026-09-03), so
    the gate accepts a whole catch-up window, never exact equality. Checks
    both today's and yesterday's occurrence of the slot, so a 23:xx slot is
    still caught just after local midnight."""
    h, m = (int(p) for p in slot.split(":"))
    for day_offset in (0, -1):
        d = now_local.date() + timedelta(days=day_offset)
        slot_dt = datetime(d.year, d.month, d.day, h, m, tzinfo=now_local.tzinfo)
        delta_minutes = (now_local - slot_dt).total_seconds() / 60
        if 0 <= delta_minutes < catch_up_minutes:
            return slot_dt
    return None


def _due_slot(cfg: Config, now_local: datetime) -> tuple[Literal["digest", "weekly"], datetime] | None:
    """(mode, slot) for the slot `now_local` falls in. With a catch-up window
    wider than the gap between two run_at slots both can match; the most
    recent one wins, so a late run is stamped with the slot it is really for."""
    starts = [s for slot in cfg.run_at if (s := _slot_start(now_local, slot, cfg.catch_up_minutes))]
    if starts:
        return "digest", max(starts)

    if cfg.weekly_review:
        day, slot = cfg.weekly_review.split(" ", 1)
        start = _slot_start(now_local, slot, cfg.catch_up_minutes)
        # weekday of the slot itself, not of now -- a "sun 23:30" slot caught
        # just after midnight is Monday by then.
        if start and day.lower() == _WEEKDAYS[start.weekday()]:
            return "weekly", start

    return None


def due(cfg: Config, now: datetime, event: str = "schedule") -> Literal["digest", "weekly"] | None:
    """Which slot (if any) `now` (tz-aware UTC) falls in, in `cfg.timezone`.

    A manual "Run workflow" click (`event == "workflow_dispatch"`) always
    returns "digest" -- a human clicked, don't gate them. If a daily and the
    weekly slot are both due in the same window, "digest" wins."""
    if event == "workflow_dispatch":
        return "digest"
    found = _due_slot(cfg, now.astimezone(local_zone(cfg.timezone)))
    return found[0] if found else None


def current_slot(cfg: Config, now: datetime, event: str = "schedule") -> datetime | None:
    """The local slot datetime this scheduled run is for -- the subject
    stamp the no-double-send guard searches for. None for a manual run
    (never stamped, never guarded) or when nothing is due. Pure, like due()."""
    if event == "workflow_dispatch":
        return None
    found = _due_slot(cfg, now.astimezone(local_zone(cfg.timezone)))
    return found[1] if found else None
