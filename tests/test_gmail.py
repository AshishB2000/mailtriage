"""gmail.py sends the digest through the user's own Gmail via SMTP. No network."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import ClassVar, cast

import pytest

from mailtriage.config import Config
from mailtriage.delivery import gmail
from mailtriage.errors import MailError
from mailtriage.models import Triaged

SENDER = "alice@gmail.com"
PW_VAR = "MAIL_PW_ALICE_GMAIL_COM"


def _item(bucket: str, subject: str = "hi", **overrides: object) -> Triaged:
    base: Triaged = {
        "bucket": bucket,
        "note": "worth a look",
        "account": "work@example.com",
        "sender": "Bob <bob@example.com>",
        "subject": subject,
        "link": "https://mail.example.com/msg/1",
        "date": "2026-08-28T00:00:00Z",
        "unread": False,
    }
    return cast(Triaged, {**base, **overrides})


def _cfg(**overrides: object) -> Config:
    cfg = Config.from_mapping({"delivery": "gmail", "email_to": SENDER, "email_from": SENDER})
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class _FakeSMTP:
    """Records .starttls/.login/.send_message; usable as `with smtplib.SMTP(...) as s:`."""

    instances: ClassVar[list[_FakeSMTP]] = []

    def __init__(self, host: str, port: int, timeout: int | None = None) -> None:
        self.host, self.port = host, port
        self.calls: list[str] = []
        self.login_args: tuple[str, str] | None = None
        self.sent_msg: EmailMessage | None = None
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:  # noqa: PYI034 -- mypy target (3.10) predates typing.Self
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, pw: str) -> None:
        self.calls.append("login")
        self.login_args = (user, pw)

    def send_message(self, msg: EmailMessage) -> None:
        self.calls.append("send_message")
        self.sent_msg = msg


@pytest.fixture(autouse=True)
def _reset_instances():
    _FakeSMTP.instances.clear()
    yield
    _FakeSMTP.instances.clear()


def test_send_logs_in_and_sends_html(monkeypatch):
    monkeypatch.setenv(PW_VAR, "app password value")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    triaged = [_item("needs_action"), _item("worth_reading")]
    gmail.send(_cfg(), triaged)

    smtp = _FakeSMTP.instances[0]
    assert smtp.calls == ["starttls", "login", "send_message"]
    assert smtp.login_args == (SENDER, "app password value")

    msg = smtp.sent_msg
    assert msg is not None
    assert msg["From"] == SENDER
    assert msg["To"] == SENDER
    body = msg.get_body(preferencelist=("html",))
    assert body is not None
    assert "hi" in body.get_content()  # subject rendered by email_html


def test_send_raises_when_app_password_missing(monkeypatch):
    monkeypatch.delenv(PW_VAR, raising=False)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    with pytest.raises(MailError, match=PW_VAR):
        gmail.send(_cfg(), [_item("worth_reading")])


def test_send_raises_mail_error_on_bad_app_password(monkeypatch):
    monkeypatch.setenv(PW_VAR, "wrong")

    class _AuthFailSMTP(_FakeSMTP):
        def login(self, user: str, pw: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(smtplib, "SMTP", _AuthFailSMTP)

    with pytest.raises(MailError, match="app password"):
        gmail.send(_cfg(), [_item("worth_reading")])


def test_send_raises_when_email_from_empty(monkeypatch):
    monkeypatch.setenv(PW_VAR, "app password value")
    with pytest.raises(MailError):
        gmail.send(_cfg(email_from=""), [_item("worth_reading")])


def test_send_raises_when_email_to_empty(monkeypatch):
    monkeypatch.setenv(PW_VAR, "app password value")
    with pytest.raises(MailError):
        gmail.send(_cfg(email_to=""), [_item("worth_reading")])
