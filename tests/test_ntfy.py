"""ntfy.py posts one markdown notification: body = digest, headers = title/click/priority."""

from __future__ import annotations

import pytest

from mailtriage.delivery import ntfy
from mailtriage.errors import MailError
from tests.helpers import capture_posts, cfg, item

TOPIC = "https://ntfy.sh/mailtriage-8f3k2"


def test_send_posts_markdown_body_and_headers(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC_URL", TOPIC)
    posts = capture_posts(monkeypatch)
    ntfy.send(
        cfg("ntfy", subject_prefix="mt"),
        [item("worth_reading", "read", link="https://r"), item("needs_action", "act", link="https://a")],
    )

    (post,) = posts
    assert post["url"] == TOPIC
    h = post["headers"]
    assert h["title"] == "mt · 1 to act · 1 to read" or h["title"].startswith("=?utf-8?")
    assert h["click"] == "https://a"  # first needs_action item, not the first item
    assert h["priority"] == "4"
    assert h["markdown"] == "yes"
    body = post["data"].decode()
    assert "**Needs action**" in body
    assert "[act](https://a)" in body
    assert body.index("act") < body.index("read")


def test_priority_is_3_without_actions(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC_URL", TOPIC)
    posts = capture_posts(monkeypatch)
    ntfy.send(cfg("ntfy"), [item("worth_reading", link="https://r")])
    assert posts[0]["headers"]["priority"] == "3"
    assert posts[0]["headers"]["click"] == "https://r"


def test_non_ascii_title_is_rfc2047_encoded(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC_URL", TOPIC)
    posts = capture_posts(monkeypatch)
    ntfy.send(cfg("ntfy", subject_prefix="mt"), [item("needs_action")])
    title = posts[0]["headers"]["title"]
    assert title.isascii()
    assert title.startswith("=?utf-8?")


def test_send_html_posts_plain_text(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC_URL", TOPIC)
    posts = capture_posts(monkeypatch)
    ntfy.send_html(cfg("ntfy"), "weekly", "<div>Your <b>week</b></div>")
    assert posts[0]["data"] == b"Your week"
    assert posts[0]["headers"]["title"] == "weekly"
    assert "click" not in posts[0]["headers"]


def test_without_topic_names_the_secret(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC_URL", raising=False)
    with pytest.raises(MailError, match="NTFY_TOPIC_URL"):
        ntfy.send(cfg("ntfy"), [item("needs_action")])


def test_refusal_raises_mail_error(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC_URL", TOPIC)
    capture_posts(monkeypatch, status=403, body="forbidden")
    with pytest.raises(MailError, match="403"):
        ntfy.send(cfg("ntfy"), [item("needs_action")])
