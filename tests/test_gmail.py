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
PW_VAR = "MAIL_PW_F24FE3C393F64986"  # pw_env_var(SENDER): BLAKE2b-128 of the address, never the address


def _item(bucket: str, subject: str = "hi", **overrides: object) -> Triaged:
    base: Triaged = {
        "bucket": bucket,
        "note": "worth a look",
        "account": "work@example.com",
        "sender": "Bob <bob@example.com>",
        "subject": subject,
        "link": "https://mail.example.com/msg/1",
        "date": "2026-08-28T00:00:00+00:00",
        "unread": False,
        "idx": 0,
        "draft": "",
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


def test_send_falls_back_to_first_mail_account_when_email_from_blank(monkeypatch):
    monkeypatch.setenv(PW_VAR, "app password value")
    monkeypatch.setenv("MAIL_ACCOUNTS", SENDER + ",bob@gmail.com")
    monkeypatch.setenv("MAIL_PW_BOB_GMAIL_COM", "bob's app password")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    gmail.send(_cfg(email_from="", email_to="someone@example.com"), [_item("worth_reading")])

    smtp = _FakeSMTP.instances[0]
    assert smtp.login_args == (SENDER, "app password value")
    assert smtp.sent_msg is not None
    assert smtp.sent_msg["From"] == SENDER


def test_send_falls_back_to_sender_when_email_to_blank(monkeypatch):
    monkeypatch.setenv(PW_VAR, "app password value")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    gmail.send(_cfg(email_to=""), [_item("worth_reading")])

    smtp = _FakeSMTP.instances[0]
    assert smtp.sent_msg is not None
    assert smtp.sent_msg["To"] == SENDER


def test_send_raises_when_email_from_blank_and_mail_accounts_unset(monkeypatch):
    monkeypatch.delenv("MAIL_ACCOUNTS", raising=False)
    with pytest.raises(MailError, match="MAIL_ACCOUNTS"):
        gmail.send(_cfg(email_from=""), [_item("worth_reading")])


def test_send_text_format_is_plain_only(monkeypatch):
    monkeypatch.setenv(PW_VAR, "app password value")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    gmail.send(_cfg(digest_format="text"), [_item("needs_action", subject="Do it")])

    msg = _FakeSMTP.instances[0].sent_msg
    assert msg is not None
    assert not msg.is_multipart()
    assert msg.get_content_type() == "text/plain"
    assert "Do it — Bob <bob@example.com> — worth a look" in msg.get_content()


def test_send_html_format_has_a_real_text_alternative(monkeypatch):
    monkeypatch.setenv(PW_VAR, "app password value")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    gmail.send(_cfg(), [_item("needs_action", subject="Do it")])

    msg = _FakeSMTP.instances[0].sent_msg
    assert msg is not None
    plain = msg.get_body(preferencelist=("plain",))
    assert plain is not None
    assert "Do it — Bob <bob@example.com>" in plain.get_content()
