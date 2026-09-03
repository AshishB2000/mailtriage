"""Telegram delivery via the Bot API.

HTML, not MarkdownV2: MarkdownV2 requires escaping 18 characters (including
``.`` ``-`` ``!``) and a single miss is a hard 400. HTML needs only
``html.escape()`` -- and it must be applied *before* wrapping in ``<b>``/``<a>``
tags, never after (`text.render` guarantees that order).
"""

from __future__ import annotations

import html
import os

from mailtriage.config import Config
from mailtriage.delivery.http import post_json
from mailtriage.delivery.mail import digest_subject
from mailtriage.delivery.text import chunk, html_to_text, render
from mailtriage.errors import MailError
from mailtriage.models import Triaged

TELEGRAM_LIMIT = 3900  # Telegram hard-fails at 4096; leave room for markup.


def telegram_html(cfg: Config, kept: list[Triaged], stamp: str = "") -> str:
    return render(
        kept,
        title=digest_subject(cfg, kept, stamp),
        bold=lambda s: f"<b>{s}</b>",
        link=lambda text, url: f'<a href="{html.escape(url, quote=True)}">{text}</a>',
        esc=html.escape,
    )


def _post(cfg: Config, text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise MailError(
            "TELEGRAM_BOT_TOKEN is not set. Get a token from @BotFather in Telegram, then add it in your fork: "
            "Settings -> Secrets and variables -> Actions -> New repository secret, named TELEGRAM_BOT_TOKEN."
        )
    chat_id = cfg.telegram_chat_id.strip()
    if not chat_id:
        raise MailError(
            "telegram_chat_id is empty in config.yaml. Send your bot any message, then open "
            "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates in a browser and copy "
            "result[0].message.chat.id into config.yaml."
        )
    for part in chunk(text, TELEGRAM_LIMIT):
        try:
            status, body = post_json(
                f"https://api.telegram.org/bot{token}/sendMessage",
                # disable_web_page_preview: else the first link renders a huge card that wrecks mobile
                {"chat_id": chat_id, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True},
            )
        except Exception as e:
            raise MailError(
                f"could not reach api.telegram.org ({type(e).__name__}: {e}). "
                "Re-run it with Actions -> digest -> Run workflow."
            ) from e
        if status >= 300:
            raise MailError(
                f"Telegram refused the message (HTTP {status}): {body}\n"
                f"  'chat not found' means telegram_chat_id ({chat_id}) is wrong, or you never sent the bot a "
                "message (a bot can't start a chat) -- open the bot, press Start, then re-read the id from "
                "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates. 'Unauthorized' means "
                "TELEGRAM_BOT_TOKEN is wrong -- get a fresh one from @BotFather."
            )


def send(cfg: Config, kept: list[Triaged], stamp: str = "") -> None:
    _post(cfg, telegram_html(cfg, kept, stamp))


def send_html(cfg: Config, subject: str, html_body: str) -> None:
    """A prebuilt HTML (the weekly review) as bold title + its plain text."""
    _post(cfg, f"<b>{html.escape(subject)}</b>\n\n{html.escape(html_to_text(html_body))}")
