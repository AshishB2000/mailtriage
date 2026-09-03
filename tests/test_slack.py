"""slack.py posts mrkdwn to an incoming webhook, split under 3000 chars."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mailtriage.delivery import slack
from mailtriage.delivery.slack import SLACK_LIMIT
from mailtriage.errors import MailError
from tests.helpers import capture_posts, cfg, item

HOOK = "https://hooks.slack.com/services/T0/B0/xyz"


def _payloads(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [json.loads(p["data"]) for p in posts]


def test_send_posts_mrkdwn_to_the_webhook(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)
    posts = capture_posts(monkeypatch)
    slack.send(cfg("slack", subject_prefix="mt"), [item("needs_action", "Title & <tag>")])

    assert posts[0]["url"] == HOOK
    assert posts[0]["headers"]["content-type"] == "application/json"
    (payload,) = _payloads(posts)
    assert set(payload) == {"text"}
    assert "*mt · 1 to act · 0 to read*" in payload["text"]
    assert "*Needs action*" in payload["text"]
    # & < > escaped BEFORE the <url|title> wrap
    assert "<https://mail.example.com/msg/1?a=1&amp;b=2|Title &amp; &lt;tag&gt;>" in payload["text"]


def test_send_chunks_a_long_digest(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)
    posts = capture_posts(monkeypatch)
    slack.send(cfg("slack"), [item("worth_reading", f"subject {i} " + "x" * 300) for i in range(40)])
    assert len(posts) > 1
    assert all(len(p["text"]) <= SLACK_LIMIT for p in _payloads(posts))


def test_send_html_is_bold_subject_plus_plain_text(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)
    posts = capture_posts(monkeypatch)
    slack.send_html(cfg("slack"), "mt · weekly review", "<div>Your <b>week</b></div>")
    assert _payloads(posts)[0]["text"] == "*mt · weekly review*\n\nYour week"


def test_without_webhook_names_the_secret(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    with pytest.raises(MailError, match="SLACK_WEBHOOK_URL"):
        slack.send(cfg("slack"), [item("needs_action")])


def test_refusal_raises_mail_error(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)
    capture_posts(monkeypatch, status=404, body="no_service")
    with pytest.raises(MailError, match="no_service"):
        slack.send(cfg("slack"), [item("needs_action")])
