"""Delivery backends. One digest goes to exactly one destination."""

from __future__ import annotations

from mailtriage.config import Config
from mailtriage.delivery import gmail, mail
from mailtriage.models import Triaged

BACKENDS = {"email": mail.send, "gmail": gmail.send}
# Same two backends, taking a prebuilt subject+HTML instead of a Triaged list
# -- the weekly review's own send path. Keeps mail.py/gmail.py as the only
# place each transport's auth/HTTP-or-SMTP logic lives.
BACKENDS_HTML = {"email": mail.send_html, "gmail": gmail.send_html}


def send(cfg: Config, triaged: list[Triaged]) -> None:
    BACKENDS[cfg.delivery](cfg, triaged)


def send_html(cfg: Config, subject: str, html: str) -> None:
    BACKENDS_HTML[cfg.delivery](cfg, subject, html)
