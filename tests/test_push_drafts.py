"""push_drafts appends AI-drafted replies to Gmail's Drafts mailbox, threaded
to the original message. It must NEVER touch INBOX and NEVER send -- see the
CRITICAL INVARIANT in imap_pull's module docstring. No real network: a fake
IMAP4_SSL stands in, mirroring how test_gmail.py fakes smtplib.SMTP.
"""

from __future__ import annotations

import imaplib
from email import message_from_bytes, policy
from typing import Any, cast

from mailtriage.imap_pull import push_drafts
from mailtriage.models import Email, Triaged

SENDER = "alice@gmail.com"
PW_VAR = "MAIL_PW_F24FE3C393F64986"  # pw_env_var(SENDER): BLAKE2b-128 of the address, never the address


def make_email(i: int, **overrides: object) -> Email:
    base: Email = {
        "account": SENDER,
        "from": f"sender{i}@example.com",
        "subject": f"real subject {i}",
        "snippet": f"real snippet {i}",
        "body": f"real body {i}",
        "date": "2026-08-28T10:00:00+00:00",
        "unread": True,
        "link": f"https://real.example.com/{i}",
        "message_id": f"<real-{i}@example.com>",
        "reply_to": f"replyto{i}@example.com",
        "uid": f"{i}",
    }
    return cast(Email, {**base, **overrides})


def make_triaged(i: int, draft: str = "a drafted reply", **overrides: object) -> Triaged:
    em = make_email(i)
    base: Triaged = {
        "bucket": "needs_action",
        "note": "reply",
        "account": em["account"],
        "sender": em["from"],
        "subject": em["subject"],
        "link": em["link"],
        "date": em["date"],
        "unread": em["unread"],
        "idx": i,
        "draft": draft,
    }
    return cast(Triaged, {**base, **overrides})


class _FakeIMAP:
    """Stand-in for imaplib.IMAP4_SSL. Records every call so tests can assert
    on exactly what push_drafts did -- including what it must never do."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        list_lines: list[bytes] | None = None,
        login_error: Exception | None = None,
        list_error: Exception | None = None,
        append_error: Exception | None = None,
    ) -> None:
        self.host, self.port = host, port
        self._list_lines = (
            list_lines if list_lines is not None else [b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"']
        )
        self._login_error = login_error
        self._list_error = list_error
        self._append_error = append_error
        self.login_calls: list[tuple[str, str]] = []
        self.select_calls: list[Any] = []  # must stay empty -- the readonly invariant
        self.appended: list[tuple[str, str, str, bytes]] = []
        self.logged_out = False

    def login(self, user: str, pw: str) -> tuple[str, list[bytes]]:
        self.login_calls.append((user, pw))
        if self._login_error:
            raise self._login_error
        return "OK", [b"Logged in"]

    def select(self, *a: object, **k: object) -> tuple[str, list[bytes]]:  # pragma: no cover
        self.select_calls.append(a)
        return "OK", [b"1"]

    def append(self, mailbox: str, flags: str, date: str, msg_bytes: bytes) -> tuple[str, list[bytes]]:
        if self._append_error:
            raise self._append_error
        self.appended.append((mailbox, flags, date, msg_bytes))
        return "OK", [b"APPEND completed"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "OK", [b"Logging out"]

    # Defined last: naming a method `list` shadows the builtin for annotations
    # appearing after it in this class body, so every other method comes first.
    def list(self, *a: object, **k: object) -> tuple[str, list[bytes]]:
        if self._list_error:
            raise self._list_error
        return "OK", self._list_lines


class _FakeIMAPFactory:
    """Callable replacement for imaplib.IMAP4_SSL that hands out configured
    _FakeIMAP instances and remembers every one it created."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.instances: list[_FakeIMAP] = []

    def __call__(self, host: str, port: int) -> _FakeIMAP:
        inst = _FakeIMAP(host, port, **self.kwargs)  # type: ignore[arg-type]
        self.instances.append(inst)
        return inst


def _patch_imap(monkeypatch: Any, **kwargs: object) -> _FakeIMAPFactory:
    factory = _FakeIMAPFactory(**kwargs)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", factory)
    return factory


def test_push_drafts_appends_with_reply_headers(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "app password")
    factory = _patch_imap(monkeypatch)

    emails = [make_email(0)]
    triaged = [make_triaged(0, draft="Sounds good.\n\nThanks,")]

    warnings = push_drafts({PW_VAR: "app password"}, triaged, emails)

    assert warnings == []
    fake = factory.instances[0]
    assert fake.login_calls == [(SENDER, "app password")]
    assert len(fake.appended) == 1
    mailbox, flags, _date, raw = fake.appended[0]
    assert mailbox == '"[Gmail]/Drafts"'
    assert flags == "\\Draft"

    msg = message_from_bytes(raw, policy=policy.default)
    assert msg["From"] == SENDER
    assert msg["To"] == emails[0]["reply_to"]
    assert msg["Subject"] == "Re: real subject 0"
    assert msg["In-Reply-To"] == emails[0]["message_id"]
    assert msg["References"] == emails[0]["message_id"]
    assert "Sounds good." in msg.get_content()


def test_push_drafts_two_variants_become_two_threaded_drafts(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    factory = _patch_imap(monkeypatch)

    emails = [make_email(0)]
    triaged = [make_triaged(0, draft="Short one.", draft_full="The full, longer reply.")]

    push_drafts({PW_VAR: "pw"}, triaged, emails)

    msgs = [message_from_bytes(raw, policy=policy.default) for _m, _f, _d, raw in factory.instances[0].appended]
    assert [m["Subject"] for m in msgs] == ["Re: real subject 0 [A short]", "Re: real subject 0 [B full]"]
    assert [m.get_content().strip() for m in msgs] == ["Short one.", "The full, longer reply."]
    assert all(m["In-Reply-To"] == emails[0]["message_id"] for m in msgs)


def test_push_drafts_discovers_localized_drafts_mailbox_name(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    factory = _patch_imap(
        monkeypatch,
        list_lines=[
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Entw&APw-rfe"',
            b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
        ],
    )

    push_drafts({PW_VAR: "pw"}, [make_triaged(0)], [make_email(0)])

    mailbox, *_rest = factory.instances[0].appended[0]
    assert mailbox == '"[Gmail]/Entw&APw-rfe"'


def test_push_drafts_falls_back_when_no_drafts_attribute(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    factory = _patch_imap(monkeypatch, list_lines=[b'(\\HasNoChildren) "/" "INBOX"'])

    push_drafts({PW_VAR: "pw"}, [make_triaged(0)], [make_email(0)])

    mailbox, *_rest = factory.instances[0].appended[0]
    assert mailbox == '"[Gmail]/Drafts"'


def test_push_drafts_does_not_double_prefix_existing_re_subject(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    factory = _patch_imap(monkeypatch)

    emails = [make_email(0, subject="RE: already a reply")]
    push_drafts({PW_VAR: "pw"}, [make_triaged(0)], emails)

    _mailbox, _flags, _date, raw = factory.instances[0].appended[0]
    msg = message_from_bytes(raw, policy=policy.default)
    assert msg["Subject"] == "RE: already a reply"


def test_push_drafts_blank_message_id_omits_reply_headers(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    factory = _patch_imap(monkeypatch)

    emails = [make_email(0, message_id="")]
    push_drafts({PW_VAR: "pw"}, [make_triaged(0)], emails)

    _mailbox, _flags, _date, raw = factory.instances[0].appended[0]
    msg = message_from_bytes(raw, policy=policy.default)
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None


def test_push_drafts_login_failure_is_a_warning_not_a_raise(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    _patch_imap(monkeypatch, login_error=OSError("login refused"))

    warnings = push_drafts({PW_VAR: "pw"}, [make_triaged(0)], [make_email(0)])  # must not raise

    assert len(warnings) == 1
    assert warnings[0]["account"] == SENDER
    assert "login refused" in warnings[0]["error"]


def test_push_drafts_append_failure_is_a_warning_not_a_raise(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    _patch_imap(monkeypatch, append_error=OSError("quota exceeded"))

    warnings = push_drafts({PW_VAR: "pw"}, [make_triaged(0)], [make_email(0)])

    assert len(warnings) == 1
    assert "quota exceeded" in warnings[0]["error"]


def test_push_drafts_missing_password_env_is_a_warning(monkeypatch: Any) -> None:
    _patch_imap(monkeypatch)  # never used -- password check must short-circuit first

    warnings = push_drafts({}, [make_triaged(0)], [make_email(0)])

    assert len(warnings) == 1
    assert warnings[0]["account"] == SENDER
    assert PW_VAR in warnings[0]["error"]


def test_push_drafts_never_selects_inbox(monkeypatch: Any) -> None:
    """The readonly invariant: push_drafts only APPENDs to Drafts, it never
    SELECTs INBOX (or anything else) at all."""
    monkeypatch.setenv(PW_VAR, "pw")
    factory = _patch_imap(monkeypatch)

    push_drafts({PW_VAR: "pw"}, [make_triaged(0)], [make_email(0)])

    assert factory.instances[0].select_calls == []


def test_push_drafts_skips_items_without_a_draft_or_not_needs_action(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    factory = _patch_imap(monkeypatch)

    emails = [make_email(0), make_email(1)]
    triaged = [
        make_triaged(0, draft=""),  # no draft -- skip
        make_triaged(1, draft="skip me", bucket="worth_reading"),  # not needs_action -- skip
    ]

    warnings = push_drafts({PW_VAR: "pw"}, triaged, emails)

    assert warnings == []
    assert factory.instances == []  # never even connects when there's nothing to push
