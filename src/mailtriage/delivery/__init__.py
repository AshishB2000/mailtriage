"""Delivery backends. One digest goes to exactly one destination."""

from __future__ import annotations

from mailtriage.config import Config
from mailtriage.delivery import discord, gmail, mail, ntfy, slack, telegram
from mailtriage.models import Event, Triaged

BACKENDS = {
    "email": mail.send,
    "gmail": gmail.send,
    "telegram": telegram.send,
    "slack": slack.send,
    "discord": discord.send,
    "ntfy": ntfy.send,
}
# Same backends, taking a prebuilt subject+HTML instead of a Triaged list --
# the weekly review's own send path. Keeps each module as the only place its
# transport's auth/HTTP-or-SMTP logic lives; the chat channels send the
# HTML's plain text (delivery.text.html_to_text).
BACKENDS_HTML = {
    "email": mail.send_html,
    "gmail": gmail.send_html,
    "telegram": telegram.send_html,
    "slack": slack.send_html,
    "discord": discord.send_html,
    "ntfy": ntfy.send_html,
}


def send(cfg: Config, triaged: list[Triaged], stamp: str = "", events: list[Event] | None = None) -> None:
    BACKENDS[cfg.delivery](cfg, triaged, stamp, events)


def send_html(cfg: Config, subject: str, html: str) -> None:
    BACKENDS_HTML[cfg.delivery](cfg, subject, html)
