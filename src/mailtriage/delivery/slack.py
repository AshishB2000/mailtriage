"""Slack delivery via an incoming webhook: POST {"text": mrkdwn}.

mrkdwn's three control characters (& < >) are escaped BEFORE the
``<url|title>`` link and ``*bold*`` wrapping, never after.
"""

from __future__ import annotations

import os

from mailtriage.config import Config
from mailtriage.delivery.http import post_json
from mailtriage.delivery.mail import digest_subject
from mailtriage.delivery.text import chunk, html_to_text, render
from mailtriage.errors import MailError
from mailtriage.models import Triaged

SLACK_LIMIT = 3000  # a webhook message over ~4000 chars is truncated; 3000 leaves room


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_mrkdwn(cfg: Config, kept: list[Triaged], stamp: str = "") -> str:
    return render(
        kept,
        title=digest_subject(cfg, kept, stamp),
        bold=lambda s: f"*{s}*",
        link=lambda text, url: f"<{_esc(url).replace('|', '%7C')}|{text}>",
        esc=_esc,
    )


def _post(text: str) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        raise MailError(
            "SLACK_WEBHOOK_URL is not set. Create an incoming webhook at https://api.slack.com/apps "
            "(your app -> Incoming Webhooks -> Add New Webhook to Workspace), then add its URL in your fork: "
            "Settings -> Secrets and variables -> Actions -> New repository secret, named SLACK_WEBHOOK_URL."
        )
    for part in chunk(text, SLACK_LIMIT):
        try:
            status, body = post_json(url, {"text": part})
        except Exception as e:
            raise MailError(
                f"could not reach the Slack webhook ({type(e).__name__}: {e}). "
                "Re-run it with Actions -> digest -> Run workflow."
            ) from e
        if status >= 300:
            raise MailError(
                f"Slack refused the message (HTTP {status}): {body}\n"
                "  'no_service' or a 404 means the webhook was deleted or the app was removed from the "
                "workspace -- create a new incoming webhook and update SLACK_WEBHOOK_URL."
            )


def send(cfg: Config, kept: list[Triaged], stamp: str = "") -> None:
    _post(slack_mrkdwn(cfg, kept, stamp))


def send_html(cfg: Config, subject: str, html_body: str) -> None:
    _post(f"*{_esc(subject)}*\n\n{_esc(html_to_text(html_body))}")
