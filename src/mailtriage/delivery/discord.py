"""Discord delivery via a channel webhook: POST {"content": markdown}.

``flags: 4`` (SUPPRESS_EMBEDS) keeps every Gmail link from unfurling into a
preview card. Markdown control characters in user text are backslash-escaped
BEFORE the ``**bold**`` and ``[title](url)`` wrapping.
"""

from __future__ import annotations

import os
import re

from mailtriage.config import Config
from mailtriage.delivery.http import post_json
from mailtriage.delivery.mail import digest_subject
from mailtriage.delivery.text import chunk, html_to_text, render
from mailtriage.errors import MailError
from mailtriage.models import Triaged

DISCORD_LIMIT = 1900  # hard cap is 2000 chars per message
SUPPRESS_EMBEDS = 4

_MD = re.compile(r"([\\*_~`|>\[\]])")


def _esc(s: str) -> str:
    return _MD.sub(r"\\\1", s)


def discord_markdown(cfg: Config, kept: list[Triaged], stamp: str = "") -> str:
    return render(
        kept,
        title=digest_subject(cfg, kept, stamp),
        bold=lambda s: f"**{s}**",
        link=lambda text, url: f"[{text}]({url.replace(')', '%29')})",
        esc=_esc,
    )


def _post(text: str) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        raise MailError(
            "DISCORD_WEBHOOK_URL is not set. In Discord: channel settings -> Integrations -> Webhooks -> "
            "New Webhook -> Copy Webhook URL, then add it in your fork: Settings -> Secrets and variables -> "
            "Actions -> New repository secret, named DISCORD_WEBHOOK_URL."
        )
    for part in chunk(text, DISCORD_LIMIT):
        try:
            status, body = post_json(url, {"content": part, "flags": SUPPRESS_EMBEDS})
        except Exception as e:
            raise MailError(
                f"could not reach the Discord webhook ({type(e).__name__}: {e}). "
                "Re-run it with Actions -> digest -> Run workflow."
            ) from e
        if status >= 300:
            raise MailError(
                f"Discord refused the message (HTTP {status}): {body}\n"
                "  A 404 'Unknown Webhook' means it was deleted in the channel's Integrations settings -- "
                "create a new one and update DISCORD_WEBHOOK_URL."
            )


def send(cfg: Config, kept: list[Triaged], stamp: str = "") -> None:
    _post(discord_markdown(cfg, kept, stamp))


def send_html(cfg: Config, subject: str, html_body: str) -> None:
    _post(f"**{_esc(subject)}**\n\n{_esc(html_to_text(html_body))}")
