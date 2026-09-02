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


def _slot_due(now_local: datetime, slot: str) -> bool:
    """True when `now_local` is within the 60 minutes after `slot` -- GitHub's
    hourly cron fires 5-30 min late, so the gate must accept the whole hour,
    never require exact equality. Checks both today's and yesterday's
    occurrence of the slot, so a 23:xx slot is still caught just after local
    midnight."""
    h, m = (int(p) for p in slot.split(":"))
    for day_offset in (0, -1):
        d = now_local.date() + timedelta(days=day_offset)
        slot_dt = datetime(d.year, d.month, d.day, h, m, tzinfo=now_local.tzinfo)
        delta_minutes = (now_local - slot_dt).total_seconds() / 60
        if 0 <= delta_minutes < 60:
            return True
    return False


def due(cfg: Config, now: datetime, event: str = "schedule") -> Literal["digest", "weekly"] | None:
    """Which slot (if any) `now` (tz-aware UTC) falls in, in `cfg.timezone`.

    A manual "Run workflow" click (`event == "workflow_dispatch"`) always
    returns "digest" -- a human clicked, don't gate them. If a daily and the
    weekly slot are both due in the same hour, "digest" wins; the weekly
    review's own send path is a later PR."""
    if event == "workflow_dispatch":
        return "digest"

    now_local = now.astimezone(local_zone(cfg.timezone))

    if any(_slot_due(now_local, slot) for slot in cfg.run_at):
        return "digest"

    if cfg.weekly_review:
        day, slot = cfg.weekly_review.split(" ", 1)
        if day.lower() == _WEEKDAYS[now_local.weekday()] and _slot_due(now_local, slot):
            return "weekly"

    return None
