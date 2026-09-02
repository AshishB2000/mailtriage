"""mail.py renders the digest HTML and posts it to Resend."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast

import pytest

from mailtriage.config import Config
from mailtriage.delivery import mail
from mailtriage.errors import MailError
from mailtriage.models import Triaged


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


def test_email_html_shows_escaped_draft():
    hostile_draft = "Sounds good <script>alert(1)</script>\n\nThanks,"
    html = mail.email_html(_cfg(), [_item("needs_action", draft=hostile_draft)])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Draft reply" in html
    assert "white-space:pre-wrap" in html


def test_email_html_omits_draft_block_when_no_draft():
    html = mail.email_html(_cfg(), [_item("needs_action", draft="")])
    assert "Draft reply" not in html


def test_email_html_carried_section_renders_with_age_and_footer():
    old_date = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    html = mail.email_html(_cfg(), [_item("carried", subject="Still open", date=old_date)])
    assert "Still waiting on you" in html
    assert "Still open" in html
    assert "3d" in html
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
