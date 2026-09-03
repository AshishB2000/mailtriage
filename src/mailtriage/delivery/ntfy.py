"""ntfy delivery: POST the digest as the body of one notification.

The topic URL is the whole credential (anyone who knows it can read and
post), so it lives in the NTFY_TOPIC_URL secret rather than config.yaml.
Headers carry the title, a tap-through link and the priority; the body is
Markdown (``Markdown: yes``).
"""

from __future__ import annotations

import os
from email.header import Header

from mailtriage.config import Config
from mailtriage.delivery.http import post
from mailtriage.delivery.mail import digest_subject
from mailtriage.delivery.text import html_to_text, render
from mailtriage.errors import MailError
from mailtriage.models import Triaged

PRIORITY_ACTION, PRIORITY_DEFAULT = "4", "3"


def _header(value: str) -> str:
    """HTTP headers are bytes; anything outside ASCII goes RFC 2047-encoded,
    which ntfy decodes (its documented way to carry a Unicode title)."""
    return value if value.isascii() else Header(value, "utf-8").encode()


def ntfy_markdown(cfg: Config, kept: list[Triaged]) -> str:
    return render(kept, bold=lambda s: f"**{s}**", link=lambda text, url: f"[{text}]({url})")


def _post(text: str, title: str, click: str, priority: str) -> None:
    url = os.environ.get("NTFY_TOPIC_URL")
    if not url:
        raise MailError(
            "NTFY_TOPIC_URL is not set. Pick a topic nobody can guess (e.g. https://ntfy.sh/mailtriage-<random>), "
            "subscribe to it in the ntfy app, then add the full URL in your fork: Settings -> Secrets and "
            "variables -> Actions -> New repository secret, named NTFY_TOPIC_URL."
        )
    headers = {"Title": _header(title), "Priority": priority, "Markdown": "yes", "Content-Type": "text/markdown"}
    if click:
        headers["Click"] = click
    try:
        status, body = post(url, text.encode("utf-8"), headers)
    except Exception as e:
        raise MailError(
            f"could not reach the ntfy server ({type(e).__name__}: {e}). "
            "Re-run it with Actions -> digest -> Run workflow."
        ) from e
    if status >= 300:
        raise MailError(
            f"ntfy refused the message (HTTP {status}): {body}\n"
            "  A 403 means the topic is reserved by another user (or needs a token) -- pick a different "
            "topic name or add ?auth=... as https://docs.ntfy.sh/publish/#access-tokens describes, and update "
            "NTFY_TOPIC_URL. A 413 means the digest is too long for the server's message limit."
        )


def send(cfg: Config, kept: list[Triaged], stamp: str = "") -> None:
    actions = [t for t in kept if t["bucket"] == "needs_action"]
    first = (actions or kept)[0]["link"] if kept else ""
    _post(
        ntfy_markdown(cfg, kept),
        digest_subject(cfg, kept, stamp),
        first,
        PRIORITY_ACTION if actions else PRIORITY_DEFAULT,
    )


def send_html(cfg: Config, subject: str, html_body: str) -> None:
    _post(html_to_text(html_body), subject, "", PRIORITY_DEFAULT)
