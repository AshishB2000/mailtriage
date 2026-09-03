"""discord.py posts markdown to a channel webhook, chunked under 2000, embeds off."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mailtriage.delivery import discord
from mailtriage.errors import MailError
from tests.helpers import capture_posts, cfg, item

HOOK = "https://discord.com/api/webhooks/123/abc"


def _payloads(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [json.loads(p["data"]) for p in posts]


def test_send_posts_content_with_embeds_suppressed(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    posts = capture_posts(monkeypatch)
    discord.send(cfg("discord", subject_prefix="mt"), [item("needs_action", "Buy *milk* [today]")])

    assert posts[0]["url"] == HOOK
    (payload,) = _payloads(posts)
    assert payload["flags"] == 4
    assert "**mt · 1 to act · 0 to read**" in payload["content"]
    assert "**Needs action**" in payload["content"]
    # markdown escaped BEFORE the [title](url) wrap
    assert "[Buy \\*milk\\* \\[today\\]](https://mail.example.com/msg/1?a=1&b=2)" in payload["content"]


def test_send_chunks_under_the_hard_cap(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    posts = capture_posts(monkeypatch)
    discord.send(cfg("discord"), [item("worth_reading", f"subject {i} " + "x" * 300) for i in range(30)])
    assert len(posts) > 1
    assert all(len(p["content"]) < 2000 for p in _payloads(posts))
    assert all(p["flags"] == 4 for p in _payloads(posts))


def test_send_html_is_bold_subject_plus_plain_text(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    posts = capture_posts(monkeypatch)
    discord.send_html(cfg("discord"), "mt · weekly review", "<div>Your <b>week</b></div>")
    assert _payloads(posts)[0]["content"] == "**mt · weekly review**\n\nYour week"


def test_without_webhook_names_the_secret(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    with pytest.raises(MailError, match="DISCORD_WEBHOOK_URL"):
        discord.send(cfg("discord"), [item("needs_action")])


def test_refusal_raises_mail_error(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    capture_posts(monkeypatch, status=404, body='{"message":"Unknown Webhook"}')
    with pytest.raises(MailError, match="Unknown Webhook"):
        discord.send(cfg("discord"), [item("needs_action")])
