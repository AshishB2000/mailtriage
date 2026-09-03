"""calendar.py: a private ICS feed -> today's events, stdlib only. The fixture
covers every shape the parser claims to support (TZID, Z, all-day, weekly
recurrence with BYDAY/UNTIL, daily with INTERVAL/COUNT, EXDATE, CANCELLED)
plus one it doesn't (MONTHLY), which must be skipped, never guessed.
"""

from __future__ import annotations

import contextlib
import urllib.request
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from mailtriage import calendar
from mailtriage.config import Config
from mailtriage.errors import MailError

TZ = ZoneInfo("Europe/Paris")
TODAY = date(2026, 9, 3)  # a Thursday

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260903T090000
DTEND;TZID=America/New_York:20260903T093000
SUMMARY:Standup
LOCATION:Room 4
URL:https://meet.example.com/standup
END:VEVENT
BEGIN:VEVENT
DTSTART:20260903T120000Z
DTEND:20260903T130000Z
SUMMARY:Lunch with Priya\\, finally
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260903
DTEND;VALUE=DATE:20260904
SUMMARY:Public holiday
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260901
DTEND;VALUE=DATE:20260905
SUMMARY:Conference week
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Europe/Paris:20260806T170000
DTEND;TZID=Europe/Paris:20260806T173000
RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261231T225959Z
SUMMARY:Weekly sync
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Europe/Paris:20260901T080000
DTEND;TZID=Europe/Paris:20260901T081500
RRULE:FREQ=DAILY;INTERVAL=2;COUNT=5
SUMMARY:Every other day
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Europe/Paris:20260827T100000
DTEND;TZID=Europe/Paris:20260827T110000
RRULE:FREQ=WEEKLY
EXDATE;TZID=Europe/Paris:20260903T100000
SUMMARY:Skipped this week
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Europe/Paris:20260903T150000
DTEND;TZID=Europe/Paris:20260903T160000
STATUS:CANCELLED
SUMMARY:Cancelled thing
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Europe/Paris:20260803T140000
DTEND;TZID=Europe/Paris:20260803T150000
RRULE:FREQ=MONTHLY;BYMONTHDAY=3
SUMMARY:Monthly (unsupported)
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Europe/Paris:20260904T090000
DTEND;TZID=Europe/Paris:20260904T100000
SUMMARY:Tomorrow
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Europe/Paris:20260903T180000
DTEND;TZID=Europe/Paris:20260903T190000
SUMMARY:A very long summary that the
  feed folded onto a second line
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n")  # real feeds are CRLF; the parser must not care


def _events() -> dict[str, Any]:
    return {ev["summary"]: ev for ev in calendar.parse_events(ICS, TODAY, TZ)}


def test_all_day_events_first_then_by_start():
    got = [ev["summary"] for ev in calendar.parse_events(ICS, TODAY, TZ)]
    assert got[:2] == ["Conference week", "Public holiday"]  # all-day, sorted by start
    assert got[2:] == [
        "Every other day",  # 08:00
        "Lunch with Priya, finally",  # 12:00Z = 14:00 Paris
        "Standup",  # 09:00 NY = 15:00 Paris
        "Weekly sync",  # 17:00
        "A very long summary that the feed folded onto a second line",  # 18:00
    ]


def test_tzid_and_utc_convert_into_cfg_timezone():
    ev = _events()
    assert ev["Standup"]["start"] == "2026-09-03T15:00:00+02:00"
    assert ev["Standup"]["end"] == "2026-09-03T15:30:00+02:00"
    assert ev["Lunch with Priya, finally"]["start"] == "2026-09-03T14:00:00+02:00"
    assert ev["Standup"]["location"] == "Room 4"
    assert ev["Standup"]["url"] == "https://meet.example.com/standup"
    assert ev["Standup"]["all_day"] is False


def test_all_day_and_multi_day_events():
    ev = _events()
    assert ev["Public holiday"] == {
        "summary": "Public holiday",
        "location": "",
        "url": "",
        "start": "2026-09-03",
        "end": "2026-09-04",
        "all_day": True,
    }
    assert ev["Conference week"]["all_day"] is True  # 1-5 Sep (DTEND exclusive) covers the 3rd
    assert "Tomorrow" not in ev


def test_weekly_recurrence_expands_to_today_at_the_original_time():
    ev = _events()["Weekly sync"]
    assert ev["start"] == "2026-09-03T17:00:00+02:00"
    assert ev["end"] == "2026-09-03T17:30:00+02:00"
    # Wednesday is not in BYDAY=TU,TH; and past UNTIL nothing fires.
    assert "Weekly sync" not in {e["summary"] for e in calendar.parse_events(ICS, date(2026, 9, 2), TZ)}
    assert "Weekly sync" not in {e["summary"] for e in calendar.parse_events(ICS, date(2027, 1, 5), TZ)}


def test_daily_interval_and_count():
    # Sep 1, 3, 5, 7, 9 are the five occurrences; the 4th and the 11th are not.
    assert "Every other day" in _events()
    for d, expected in ((date(2026, 9, 4), False), (date(2026, 9, 9), True), (date(2026, 9, 11), False)):
        assert ("Every other day" in {e["summary"] for e in calendar.parse_events(ICS, d, TZ)}) is expected


def test_exdate_cancelled_and_unsupported_rules_are_skipped():
    ev = _events()
    assert "Skipped this week" not in ev
    assert "Skipped this week" in {e["summary"] for e in calendar.parse_events(ICS, date(2026, 9, 10), TZ)}
    assert "Cancelled thing" not in ev
    assert "Monthly (unsupported)" not in ev


def test_unknown_tzid_falls_back_to_cfg_timezone():
    ics = "BEGIN:VEVENT\nDTSTART;TZID=Mars/Olympus:20260903T090000\nSUMMARY:x\nEND:VEVENT\n"
    ev = calendar.parse_events(ics, TODAY, TZ)[0]
    assert ev["start"] == "2026-09-03T09:00:00+02:00"
    assert ev["end"] == ev["start"]  # no DTEND: zero-length


def test_garbage_is_not_an_event():
    assert calendar.parse_events("BEGIN:VEVENT\nDTSTART:soon\nSUMMARY:x\nEND:VEVENT\n", TODAY, TZ) == []
    assert calendar.parse_events("", TODAY, TZ) == []


# --- today_events: the stage --------------------------------------------


def _cfg(**kw: Any) -> Config:
    return Config(delivery="email", timezone="Europe/Paris", **kw)


NOW = datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc)


def test_today_events_without_the_secret_is_empty_and_silent(capsys: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(calendar, "fetch_ics", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
    assert calendar.today_events({}, _cfg(), NOW) == []
    assert calendar.today_events({"CALENDAR_ICS_URL": "https://x"}, _cfg(calendar=False), NOW) == []
    assert capsys.readouterr().err == ""


def test_today_events_fetches_and_parses(capsys: Any, monkeypatch: Any) -> None:
    seen: list[str] = []

    def fake_fetch(url: str) -> str:
        seen.append(url)
        return ICS

    monkeypatch.setattr(calendar, "fetch_ics", fake_fetch)
    events = calendar.today_events({"CALENDAR_ICS_URL": " https://cal.example/private.ics "}, _cfg(), NOW)
    assert seen == ["https://cal.example/private.ics"]
    assert [e["summary"] for e in events][:2] == ["Conference week", "Public holiday"]
    err = capsys.readouterr().err
    assert "7 calendar event(s) today." in err
    assert "Standup" not in err


def test_today_events_failure_warns_by_type_only_and_never_raises(capsys: Any, monkeypatch: Any) -> None:
    def boom(url: str) -> str:
        raise OSError(f"could not reach {url}")

    monkeypatch.setattr(calendar, "fetch_ics", boom)
    assert calendar.today_events({"CALENDAR_ICS_URL": "https://secret.example/x"}, _cfg(), NOW) == []
    err = capsys.readouterr().err
    assert "calendar fetch failed (OSError), skipping." in err
    assert "secret.example" not in err  # the URL is the credential


def test_fetch_ics_caps_the_body(monkeypatch: Any) -> None:
    class _Resp:
        def read(self, n: int) -> bytes:
            return b"x" * n

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: contextlib.nullcontext(_Resp()))
    with pytest.raises(MailError, match="larger than"):
        calendar.fetch_ics("https://x", cap=10)
