"""mail.py renders the digest HTML and posts it to Resend."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import cast

import pytest

from mailtriage.config import Config
from mailtriage.delivery import mail
from mailtriage.errors import MailError
from mailtriage.models import Event, Triaged, WeekItem, WeekResult


def _item(bucket: str, subject: str = "hi", **overrides: object) -> Triaged:
    base: Triaged = {
        "bucket": bucket,
        "note": "worth a look",
        "account": "work@example.com",
        "sender": "Alice <alice@example.com>",
        "subject": subject,
        "link": "https://mail.example.com/msg/1",
        "date": "2026-08-28T00:00:00+00:00",
        "unread": False,
        "idx": 0,
        "draft": "",
    }
    return cast(Triaged, {**base, **overrides})


def _cfg(**overrides: object) -> Config:
    cfg = Config.from_mapping({"delivery": "email", "email_to": "me@example.com", "email_from": "bot@example.com"})
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_email_html_escapes_subject():
    html = mail.email_html(_cfg(), [_item("needs_action", subject="<script>alert(1)</script>")])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_email_html_shows_both_sections_when_both_present():
    html = mail.email_html(_cfg(), [_item("needs_action"), _item("worth_reading")])
    assert "Needs action" in html
    assert "Worth reading" in html


def test_email_html_omits_needs_action_when_empty():
    html = mail.email_html(_cfg(), [_item("worth_reading")])
    assert "Needs action" not in html
    assert "Worth reading" in html


def test_send_posts_list_recipient_and_bucket_counts(monkeypatch):
    captured = {}

    def fake_post_json(url, payload, headers=None):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        return 200, "{}"

    monkeypatch.setattr(mail, "post_json", fake_post_json)
    monkeypatch.setenv("RESEND_API_KEY", "test-key")

    cfg = _cfg(subject_prefix="mailtriage")
    triaged = [_item("needs_action"), _item("needs_action"), _item("worth_reading")]
    mail.send(cfg, triaged)

    assert captured["payload"]["to"] == ["me@example.com"]
    assert isinstance(captured["payload"]["to"], list)
    assert captured["payload"]["subject"] == "mailtriage · 2 to act · 1 to read"


def test_send_raises_mail_error_on_403(monkeypatch):
    monkeypatch.setattr(mail, "post_json", lambda *a, **k: (403, "domain not verified"))
    monkeypatch.setenv("RESEND_API_KEY", "test-key")

    with pytest.raises(MailError):
        mail.send(_cfg(), [_item("worth_reading")])


def test_send_html_posts_given_subject_and_body(monkeypatch):
    captured = {}

    def fake_post_json(url, payload, headers=None):
        captured["payload"] = payload
        return 200, "{}"

    monkeypatch.setattr(mail, "post_json", fake_post_json)
    monkeypatch.setenv("RESEND_API_KEY", "test-key")

    mail.send_html(_cfg(), "mailtriage · weekly review", "<p>hi</p>")

    assert captured["payload"]["subject"] == "mailtriage · weekly review"
    assert captured["payload"]["html"] == "<p>hi</p>"
    assert captured["payload"]["to"] == ["me@example.com"]


# --- weekly_html ---------------------------------------------------------


def _week_item(subject: str = "hi", **overrides: object) -> WeekItem:
    base: WeekItem = {
        "account": "work@example.com",
        "sender": "Alice <alice@example.com>",
        "subject": subject,
        "date": "2026-08-25T00:00:00+00:00",
        "link": "https://mail.example.com/msg/1",
        "age_days": 3,
    }
    return cast(WeekItem, {**base, **overrides})


def _week(accounts: dict[str, dict[str, list[WeekItem]]]) -> WeekResult:
    return {"accounts": accounts, "warnings": []}


def test_weekly_html_shows_totals_and_headline():
    week = _week(
        {
            "work@example.com": {
                "replied": [_week_item("replied one")],
                "archived": [_week_item("archived one")],
                "open": [_week_item("open one")],
            }
        }
    )
    html = mail.weekly_html(_cfg(), week)
    assert "Your week" in html
    assert "2 handled" in html
    assert "1 still open" in html


def test_weekly_html_renders_open_items_oldest_first():
    week = _week(
        {
            "work@example.com": {
                "replied": [],
                "archived": [],
                "open": [
                    _week_item("newer", date="2026-08-27T00:00:00+00:00", age_days=1),
                    _week_item("older", date="2026-08-20T00:00:00+00:00", age_days=8),
                ],
            }
        }
    )
    html = mail.weekly_html(_cfg(), week)
    assert html.index("older") < html.index("newer")
    assert "8d" in html


def test_weekly_html_shows_account_counts_and_recent_subjects():
    week = _week(
        {
            "work@example.com": {
                "replied": [_week_item("replied subject")],
                "archived": [],
                "open": [],
            }
        }
    )
    html = mail.weekly_html(_cfg(), week)
    assert "work@example.com" in html
    assert "1 replied" in html
    assert "0 archived" in html
    assert "0 open" in html
    assert "replied subject" in html


def test_weekly_html_footer_names_the_label():
    week = _week({"work@example.com": {"replied": [], "archived": [], "open": [_week_item()]}})
    html = mail.weekly_html(_cfg(label="mailtriage/action"), week)
    assert "Open items clear when you reply, archive, or remove the mailtriage/action label." in html


def test_weekly_html_escapes_hostile_subject_and_label():
    week = _week(
        {"work@example.com": {"replied": [], "archived": [], "open": [_week_item("<script>alert(1)</script>")]}}
    )
    html = mail.weekly_html(_cfg(label="<script>alert(2)</script>"), week)
    assert "<script>alert(1)</script>" not in html
    assert "<script>alert(2)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html


def test_weekly_html_no_open_items_shows_placeholder():
    week = _week({"work@example.com": {"replied": [_week_item()], "archived": [], "open": []}})
    html = mail.weekly_html(_cfg(), week)
    assert "Nothing open." in html


def test_email_html_shows_escaped_draft():
    hostile_draft = "Sounds good <script>alert(1)</script>\n\nThanks,"
    html = mail.email_html(_cfg(), [_item("needs_action", draft=hostile_draft)])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Draft reply" in html
    assert "white-space:pre-wrap" in html


def test_email_html_notes_fuller_draft_only_when_present():
    with_full = mail.email_html(_cfg(), [_item("needs_action", draft="short", draft_full="long")])
    assert "A fuller version is in Drafts." in with_full
    without = mail.email_html(_cfg(), [_item("needs_action", draft="short")])
    assert "fuller version" not in without


def test_email_html_noise_footer_is_folded_and_only_https_or_mailto():
    items = [
        _item("needs_action"),
        _item("noise", sender="Deals <deals@shop.com>", link="https://shop.com/unsub?u=1"),
        _item("noise", sender="List", link="mailto:leave@list.org"),
        _item("noise", sender="Bad", link="http://insecure.example/unsub"),
        _item("noise", sender="Worse", link="javascript:alert(1)"),
    ]
    html = mail.email_html(_cfg(), items)
    assert "<details>" in html and "Noise this week" in html
    assert 'href="https://shop.com/unsub?u=1"' in html and "Deals &lt;deals@shop.com&gt;" in html
    assert 'href="mailto:leave@list.org"' in html
    assert "insecure.example" not in html and "javascript:" not in html
    assert html.count(">Unsubscribe</a>") == 2


def test_email_html_no_noise_footer_when_no_noise():
    html = mail.email_html(_cfg(), [_item("needs_action")])
    assert "Noise this week" not in html and "<details>" not in html


def test_email_html_omits_draft_block_when_no_draft():
    html = mail.email_html(_cfg(), [_item("needs_action", draft="")])
    assert "Draft reply" not in html


def test_email_html_carried_section_renders_with_age_and_footer():
    old_date = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    html = mail.email_html(_cfg(), [_item("carried", subject="Still open", date=old_date)])
    assert "Still waiting on you" in html
    assert "Still open" in html
    assert "waiting 3 days" in html
    assert "Clears when you reply, archive, or remove the mailtriage/action label in Gmail." in html


def test_email_html_omits_carried_section_when_empty():
    html = mail.email_html(_cfg(), [_item("worth_reading")])
    assert "Still waiting on you" not in html


def test_email_html_carried_section_escapes_subject_and_label():
    html = mail.email_html(
        _cfg(label="<script>alert(2)</script>"),
        [_item("carried", subject="<script>alert(1)</script>")],
    )
    assert "<script>alert(1)</script>" not in html
    assert "<script>alert(2)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html


# --- numbering, deadlines, nag, command hint (sub-project B) ------------


def test_email_html_numbers_items_in_section_order_as_links():
    html = mail.email_html(
        _cfg(),
        [
            _item("worth_reading", subject="zeta"),
            _item("needs_action", subject="alpha"),
            _item("carried", subject="omega"),
        ],
    )
    # needs_action, then carried, then worth_reading -- #1 alpha, #2 omega, #3 zeta
    assert html.index(">#1</a>") < html.index("alpha") < html.index(">#2</a>") < html.index("omega")
    assert html.index("omega") < html.index(">#3</a>") < html.index("zeta")
    assert '<a href="https://mail.example.com/msg/1" class="muted"' in html


def test_email_html_groups_needs_action_by_due_and_links_calendar():
    today = date(2026, 9, 3)
    items = [
        _item("needs_action", subject="later one", due="2026-09-20"),
        _item("needs_action", subject="no date one"),
        _item("needs_action", subject="overdue one", due="2026-09-01"),
        _item("needs_action", subject="today one", due="2026-09-03"),
        _item("needs_action", subject="week one", due="2026-09-08"),
    ]
    html = mail.email_html(_cfg(), items, today=today)
    order = [
        html.index(s)
        for s in (
            "Overdue",
            "overdue one",
            "Today",
            "today one",
            "This week",
            "week one",
            "Later",
            "later one",
            "No date",
            "no date one",
        )
    ]
    assert order == sorted(order)
    assert "Due Tue 1 Sep" in html
    cal = mail.calendar_link(items[2])
    assert cal.startswith(
        "https://calendar.google.com/calendar/render?action=TEMPLATE&text=overdue+one&dates=20260901%2F20260902"
    )
    assert "details=https%3A%2F%2Fmail.example.com%2Fmsg%2F1" in cal
    assert "Add to Google Calendar" in html


def test_email_html_without_due_dates_has_no_groups():
    html = mail.email_html(_cfg(), [_item("needs_action")])
    assert "Needs action" in html
    assert "No date" not in html and "Overdue" not in html


def test_digest_groups_sorts_dated_items_by_due():
    items = [_item("needs_action", subject="b", due="2026-09-30"), _item("needs_action", subject="a", due="2026-09-25")]
    groups = mail.digest_groups(items, date(2026, 9, 3))
    assert [(k, h) for k, h, _ in groups] == [("action", "Needs action · Later")]
    assert [t["subject"] for t in groups[0][2]] == ["a", "b"]


def test_email_html_carried_nag_badge_after_nag_after_days():
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    html = mail.email_html(
        _cfg(nag_after_days=3),
        [_item("carried", subject="stale", date=old), _item("carried", subject="new", date=fresh)],
    )
    assert "waiting 5 days" in html and "waiting 1 day<" in html
    assert html.count("still open") == 1
    assert html.index("stale") < html.index("still open") < html.index("new")


def test_email_html_footer_explains_label_and_reply_commands():
    html = mail.email_html(_cfg(), [_item("needs_action")])
    for needle in (
        "mailtriage/done",
        "mailtriage/snooze-3d",
        "mailtriage/never",
        "mailtriage/vip",
        "&quot;done 2&quot;",
    ):
        assert needle in html


# --- Today block (sub-project F) ------------------------------------------


def _event(summary: str, start: str = "2026-09-03T09:00:00+02:00", end: str = "", **over: object) -> Event:
    base: Event = {
        "summary": summary,
        "location": "",
        "url": "",
        "start": start,
        "end": end or start,
        "all_day": False,
    }
    return cast(Event, {**base, **over})


def test_email_html_today_block_sits_above_mail_and_links_the_invite():
    events = [
        _event("Holiday", start="2026-09-03", end="2026-09-04", all_day=True),
        _event("Standup", end="2026-09-03T09:30:00+02:00", location="Room 4", url="https://meet.example/x"),
    ]
    items = [
        _item("worth_reading", subject="a read"),
        _item("needs_action", subject="Invitation: Standup @ Thu Sep 3, 2026 9am (alice@example.com)"),
    ]
    html = mail.email_html(_cfg(), items, today=date(2026, 9, 3), events=events)
    assert html.index("Today") < html.index("Holiday") < html.index("Standup") < html.index("Needs action")
    assert "All day" in html and "09:00–09:30" in html and "Room 4" in html
    assert 'href="https://meet.example/x"' in html
    assert "invite in your inbox: #1" in html  # needs_action is #1, worth_reading is #2


def test_email_html_today_block_escapes_and_caps():
    events = [_event(f"<b>{i}</b>") for i in range(20)]
    html = mail.email_html(_cfg(), [_item("needs_action")], events=events)
    assert "<b>0</b>" not in html and "&lt;b&gt;0&lt;/b&gt;" in html
    assert "&lt;b&gt;11&lt;/b&gt;" in html and "&lt;b&gt;12&lt;/b&gt;" not in html


def test_email_html_without_events_has_no_today_block():
    assert "Today" not in mail.email_html(_cfg(), [_item("needs_action")])


def test_invite_numbers_only_matches_invitation_looking_needs_action():
    events = [_event("Standup"), _event("Retro")]
    items = [
        _item("needs_action", subject="Re: Standup notes"),  # not an invitation
        _item("worth_reading", subject="Invitation: Retro"),  # wrong bucket
        _item("needs_action", subject="Updated invitation: Standup @ Fri"),
    ]
    assert mail.invite_numbers(events, items, date(2026, 9, 3)) == {0: 2}


def test_weekly_html_shows_done_count_only_when_nonzero():
    week = _week({"work@example.com": {"replied": [], "archived": [], "open": [_week_item()]}})
    assert "marked done" not in mail.weekly_html(_cfg(), week)
    assert "4 marked done" in mail.weekly_html(_cfg(), week, done_count=4)


# --- digest_format ---------------------------------------------------------


def test_send_html_format_carries_a_text_part_too(monkeypatch):
    captured = {}
    monkeypatch.setattr(mail, "post_json", lambda url, payload, headers=None: captured.update(payload) or (200, "{}"))
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    mail.send(_cfg(), [_item("needs_action", subject="Do it")])
    assert "<html" in captured["html"]
    assert "Do it — Alice <alice@example.com> — worth a look" in captured["text"]


def test_send_text_format_omits_html(monkeypatch):
    captured = {}
    monkeypatch.setattr(mail, "post_json", lambda url, payload, headers=None: captured.update(payload) or (200, "{}"))
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    mail.send(_cfg(digest_format="text"), [_item("needs_action", subject="Do it")])
    assert "html" not in captured
    assert captured["text"].startswith("Needs action\n\nDo it — ")
    assert captured["subject"] == "mailtriage · 1 to act · 0 to read"


def test_send_html_derives_its_text_part_from_the_html(monkeypatch):
    captured = {}
    monkeypatch.setattr(mail, "post_json", lambda url, payload, headers=None: captured.update(payload) or (200, "{}"))
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    mail.send_html(_cfg(), "weekly", "<div>Your <b>week</b></div>")
    assert captured["text"] == "Your week"
