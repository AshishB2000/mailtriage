"""Today's calendar, for the top of the morning brief. Stdlib only.

The ICS feed URL lives in the CALENDAR_ICS_URL secret (Google Calendar's
"Secret address in iCal format") -- a secret because the URL *is* the auth.
Nothing here ever prints it, or an error message that might embed it: a
fetch failure is reported by exception type name only.

Parsing is deliberately narrow: VEVENTs with DTSTART/DTEND (TZID=, Z, and
all-day DATE forms), SUMMARY, LOCATION, URL, STATUS:CANCELLED, EXDATE, and
RRULE with FREQ=DAILY or FREQ=WEEKLY (INTERVAL/BYDAY/UNTIL/COUNT) expanded
for today only. Anything else -- MONTHLY/YEARLY rules, BYMONTHDAY, RDATE,
DURATION, RECURRENCE-ID overrides -- is unsupported and that event is
skipped, never guessed at. A brief that omits an event beats one that
invents a time.
"""

from __future__ import annotations

import re
import sys
import urllib.request
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mailtriage.config import Config
from mailtriage.delivery.http import UA
from mailtriage.errors import MailError
from mailtriage.models import Event
from mailtriage.schedule import local_zone

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 20

_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def fetch_ics(url: str, timeout: int = TIMEOUT, cap: int = MAX_BYTES) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data: bytes = resp.read(cap + 1)
    if len(data) > cap:
        raise MailError(f"calendar feed is larger than {cap // (1024 * 1024)} MB")
    return data.decode("utf-8", "replace")


# --- parsing (pure) -------------------------------------------------------


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split_property(line: str) -> tuple[str, dict[str, str], str]:
    """'DTSTART;TZID=Europe/Paris:20260903T090000' -> ("DTSTART", {"TZID": "Europe/Paris"}, "2026...").
    The first ':' outside double quotes ends the name+params part."""
    quoted, i = False, 0
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            break
    else:
        return line.upper(), {}, ""
    head, value = line[:i], line[i + 1 :]
    name, *params = head.split(";")
    out: dict[str, str] = {}
    for p in params:
        k, _, v = p.partition("=")
        out[k.upper()] = v.strip('"')
    return name.upper(), out, value


def _vevents(lines: list[str]) -> list[list[tuple[str, dict[str, str], str]]]:
    events: list[list[tuple[str, dict[str, str], str]]] = []
    cur: list[tuple[str, dict[str, str], str]] | None = None
    for line in lines:
        if line.upper() == "BEGIN:VEVENT":
            cur = []
        elif line.upper() == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None:
            cur.append(_split_property(line))
    return events


def _parse_dt(value: str, params: dict[str, str], tz: Any) -> tuple[datetime | date, bool] | None:
    """(datetime in `tz`, all_day=False) or (date, all_day=True); None when unparseable.
    Z -> UTC; TZID= -> that zone (unknown zone falls back to `tz`); naive -> `tz`."""
    v = value.strip()
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z?))?", v)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        if params.get("VALUE") == "DATE" or m.group(4) is None:
            return date(y, mo, d), True
        zone = tz
        if m.group(7):
            zone = timezone.utc
        elif "TZID" in params:
            try:
                zone = ZoneInfo(params["TZID"])
            except (ZoneInfoNotFoundError, ValueError):
                zone = tz  # an unknown zone name reads as local rather than dropping the event
        local = datetime(y, mo, d, int(m.group(4)), int(m.group(5)), int(m.group(6) or 0), tzinfo=zone)
    except ValueError:
        return None
    return local.astimezone(tz), False


def _rrule(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in value.split(";"):
        k, _, v = part.partition("=")
        if k:
            out[k.upper()] = v.upper()
    return out


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _rule_hits(rule: dict[str, str], start: date, d: date) -> bool:
    """Does a DAILY/WEEKLY rule (INTERVAL/BYDAY, no COUNT/UNTIL) hit `d`?"""
    if d < start:
        return False
    interval = max(1, int(rule.get("INTERVAL") or 1))
    if rule["FREQ"] == "DAILY":
        return (d - start).days % interval == 0
    bydays = [b[-2:] for b in rule.get("BYDAY", "").split(",") if b] or [_WEEKDAYS[start.weekday()]]
    if _WEEKDAYS[d.weekday()] not in bydays:
        return False
    return (_monday(d) - _monday(start)).days // 7 % interval == 0


def _recurs_today(rule: dict[str, str], start: date, today: date, tz: Any) -> bool | None:
    """True/False for a supported rule; None for an unsupported one (skip the event)."""
    if rule.get("FREQ") not in ("DAILY", "WEEKLY"):
        return None
    if not _rule_hits(rule, start, today):
        return False
    if rule.get("UNTIL"):
        parsed = _parse_dt(rule["UNTIL"], {}, tz)
        if parsed is None:
            return None
        if today > _as_date(parsed):
            return False
    if rule.get("COUNT"):
        # ponytail: day-by-day scan bounded at ~27 years; a COUNT rule that
        # old is treated as exhausted rather than expanded in closed form.
        n, d, budget = 0, start, 10000
        while d < today and budget:
            n += _rule_hits(rule, start, d)
            d += timedelta(days=1)
            budget -= 1
        if not budget or n >= int(rule["COUNT"]):
            return False
    return True


def _text(props: list[tuple[str, dict[str, str], str]], name: str) -> str:
    for n, _p, v in props:
        if n == name:
            return v.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").replace("\\N", " ").strip()
    return ""


def parse_events(text: str, today: date, tz: Any) -> list[Event]:
    """Today's events, all-day first then by start time. Pure."""
    out: list[tuple[int, str, Event]] = []
    for props in _vevents(_unfold(text)):
        if _text(props, "STATUS").upper() == "CANCELLED":
            continue
        start_prop = next(((p, v) for n, p, v in props if n == "DTSTART"), None)
        if start_prop is None:
            continue
        parsed = _parse_dt(start_prop[1], start_prop[0], tz)
        if parsed is None:
            continue
        start, all_day = parsed
        end_prop = next(((p, v) for n, p, v in props if n == "DTEND"), None)
        end_parsed = _parse_dt(end_prop[1], end_prop[0], tz) if end_prop else None
        end: datetime | date = end_parsed[0] if end_parsed else start
        start_date = start if isinstance(start, date) and not isinstance(start, datetime) else start.date()
        duration = end - start if type(end) is type(start) and end > start else timedelta(0)

        rrule = next((v for n, _p, v in props if n == "RRULE"), "")
        if rrule:
            hit = _recurs_today(_rrule(rrule), start_date, today, tz)
            if not hit:
                continue  # False: not today; None: unsupported rule, skipped on purpose
            if all_day:
                start, end = today, today + duration
            else:
                assert isinstance(start, datetime)
                start = datetime.combine(today, start.timetz())
                end = start + duration
        elif all_day:
            last = start_date + max(duration - timedelta(days=1), timedelta(0))
            if not start_date <= today <= last:
                continue
        else:
            assert isinstance(start, datetime)
            last = (start + duration - timedelta(microseconds=1)).date() if duration else start_date
            if not start_date <= today <= last:
                continue

        exdates = {
            _as_date(x)
            for n, p, v in props
            if n == "EXDATE"
            for raw in v.split(",")
            if (x := _parse_dt(raw, p, tz)) is not None
        }
        if today in exdates:
            continue

        ev: Event = {
            "summary": _text(props, "SUMMARY"),
            "location": _text(props, "LOCATION"),
            "url": _text(props, "URL"),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "all_day": all_day,
        }
        out.append((0 if all_day else 1, ev["start"], ev))
    out.sort(key=lambda t: (t[0], t[1]))
    return [ev for _a, _s, ev in out]


def _as_date(parsed: tuple[datetime | date, bool]) -> date:
    v = parsed[0]
    return v.date() if isinstance(v, datetime) else v


# --- the stage --------------------------------------------------------------


def today_events(environ: Mapping[str, str], cfg: Config, now: datetime) -> list[Event]:
    """Today's events in cfg.timezone, or [] -- a missing secret is simply
    "no calendar", and any fetch/parse failure warns (type name only, the
    URL is a secret) and never fails the run."""
    url = (environ.get("CALENDAR_ICS_URL") or "").strip()
    if not cfg.calendar or not url:
        return []
    tz = local_zone(cfg.timezone)
    try:
        events = parse_events(fetch_ics(url), now.astimezone(tz).date(), tz)
    except Exception as e:  # urlopen raises 6+ unrelated types; the brief must still go out
        print(f"mailtriage: calendar fetch failed ({type(e).__name__}), skipping.", file=sys.stderr)
        return []
    print(f"mailtriage: {len(events)} calendar event(s) today.", file=sys.stderr)
    return events
