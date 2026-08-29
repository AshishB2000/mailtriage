"""Delivery backends. One digest goes to exactly one destination."""

from __future__ import annotations

from mailtriage.config import Config
from mailtriage.delivery import mail
from mailtriage.models import Triaged

BACKENDS = {"email": mail.send}


def send(cfg: Config, triaged: list[Triaged]) -> None:
    BACKENDS[cfg.delivery](cfg, triaged)
