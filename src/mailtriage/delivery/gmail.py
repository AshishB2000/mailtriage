"""Email delivery through the user's own Gmail via SMTP.

Reuses the same app-password secret imap_pull already reads that inbox with,
so sending needs no new setup for anyone who already reads via that account.
Stdlib only.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from mailtriage.config import Config
from mailtriage.delivery.mail import digest_subject, email_html
from mailtriage.delivery.text import digest_text, html_to_text
from mailtriage.errors import MailError
from mailtriage.imap_pull import accounts_from_env, app_password, pw_env_var
from mailtriage.models import Event, Triaged


def _send(cfg: Config, subject: str, text: str, html_body: str | None) -> None:
    """Send through the user's own Gmail SMTP. Shared transport for the
    normal digest (`send`) and the weekly review (delivery.send_html ->
    `send_html`), so the account-resolution/auth/SMTP logic lives in exactly
    one place. Always carries a plain-text part; the HTML alternative is
    skipped for `digest_format: text`."""
    to, sender = cfg.email_to.strip(), cfg.email_from.strip()
    if not sender:
        try:
            sender = accounts_from_env(os.environ)[0][0]
        except MailError as e:
            raise MailError(
                "email_from is empty. Set the EMAIL_FROM secret, email_from in config.yaml, or fall back to "
                f"the first MAIL_ACCOUNTS address ({e})"
            ) from e
    if not to:
        to = sender

    var = pw_env_var(sender)
    pw = app_password(os.environ, sender)
    if not pw:
        raise MailError(
            f"{sender}: no app password found in ${var}. Create one at myaccount.google.com/apppasswords. "
            f"If {sender} is one of your MAIL_ACCOUNTS, this is the same secret used to read that inbox."
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(sender, pw)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise MailError(
            f"Gmail rejected the app password for {sender}. It may be wrong or revoked — create a fresh one "
            f"at myaccount.google.com/apppasswords (the 16-character value, spaces stripped) and update ${var}."
        ) from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"could not send via Gmail SMTP ({type(e).__name__}: {e}). Re-run the workflow.") from e


def send_html(cfg: Config, subject: str, html_body: str) -> None:
    _send(cfg, subject, html_to_text(html_body), html_body)


def send(cfg: Config, triaged: list[Triaged], stamp: str = "", events: list[Event] | None = None) -> None:
    html_body = None if cfg.digest_format == "text" else email_html(cfg, triaged, events=events)
    _send(cfg, digest_subject(cfg, triaged, stamp), digest_text(triaged), html_body)
