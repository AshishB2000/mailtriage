"""telegram.py posts HTML-mode messages to the Bot API, chunked under 4096."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mailtriage.config import Config
from mailtriage.delivery import telegram
from mailtriage.delivery.telegram import TELEGRAM_LIMIT
from mailtriage.errors import MailError
from tests.helpers import capture_posts, cfg, item


def _cfg(**over: object) -> Config:
    return cfg("telegram", **{"telegram_chat_id": "12345", **over})


def _payloads(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [json.loads(p["data"]) for p in posts]


def test_send_posts_html_to_the_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    posts = capture_posts(monkeypatch)
    telegram.send(_cfg(subject_prefix="mt"), [item("needs_action", "Title & <tag>")])

    assert posts[0]["url"] == "https://api.telegram.org/bottok/sendMessage"
    (payload,) = _payloads(posts)
    assert payload["chat_id"] == "12345"
    assert payload["parse_mode"] == "HTML"
    assert payload["disable_web_page_preview"] is True
    assert "<b>mt · 1 to act · 0 to read</b>" in payload["text"]
    # escaped BEFORE the <a> wrap, never after
    assert '<a href="https://mail.example.com/msg/1?a=1&amp;b=2">Title &amp; &lt;tag&gt;</a>' in payload["text"]
    assert "<b>Needs action</b>" in payload["text"]


def test_send_carries_the_slot_stamp(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    posts = capture_posts(monkeypatch)
    telegram.send(_cfg(subject_prefix="mt"), [item("needs_action")], stamp="Thu 03 Sep 08:00")
    assert "<b>mt · Thu 03 Sep 08:00 · 1 to act · 0 to read</b>" in _payloads(posts)[0]["text"]


def test_send_chunks_a_long_digest(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    posts = capture_posts(monkeypatch)
    telegram.send(_cfg(), [item("worth_reading", f"subject {i} " + "x" * 300) for i in range(40)])
    assert len(posts) > 1
    assert all(len(p["text"]) <= TELEGRAM_LIMIT for p in _payloads(posts))
    assert all(p["chat_id"] == "12345" for p in _payloads(posts))


def test_send_html_sends_bold_subject_and_plain_text(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    posts = capture_posts(monkeypatch)
    telegram.send_html(_cfg(), "mt · weekly review", "<div>Your <b>week</b> &amp; more</div>")
    text = _payloads(posts)[0]["text"]
    assert text.startswith("<b>mt · weekly review</b>\n\n")
    assert "Your week &amp; more" in text
    assert "<div>" not in text


def test_without_token_names_the_secret(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(MailError, match="TELEGRAM_BOT_TOKEN"):
        telegram.send(_cfg(), [item("needs_action")])


def test_without_chat_id_names_the_setting(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    with pytest.raises(MailError, match="telegram_chat_id"):
        telegram.send(_cfg(telegram_chat_id=""), [item("needs_action")])


def test_refusal_names_the_fix(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    capture_posts(monkeypatch, status=400, body='{"description":"Bad Request: chat not found"}')
    with pytest.raises(MailError, match="chat not found"):
        telegram.send(_cfg(), [item("needs_action")])
