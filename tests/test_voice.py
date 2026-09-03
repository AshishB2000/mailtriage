"""pull_voice_examples reads the reader's own recent Sent messages to a
draft's recipient so the drafting prompt can match their voice. Read-only
(\\Sent selected readonly, BODY.PEEK), and the text must reach the prompt
only -- never a log. No real network: a fake IMAP4_SSL stands in.
"""

from __future__ import annotations

import imaplib
from typing import Any, cast

from mailtriage.imap_pull import pull_voice_examples, pw_env_var, reply_text
from mailtriage.models import Email, Triaged

ACCOUNT = "alice@gmail.com"
PW_VAR = "MAIL_PW_ALICE_GMAIL_COM"
ENV = {PW_VAR: "pw"}


def _sent(to: str, body: str) -> bytes:
    return (
        f"From: {ACCOUNT}\r\nTo: {to}\r\nSubject: Re: x\r\nDate: Fri, 28 Aug 2026 09:00:00 +0000\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


def make_email(i: int, reply_to: str) -> Email:
    return cast(
        Email,
        {
            "account": ACCOUNT,
            "from": reply_to,
            "subject": f"subject {i}",
            "snippet": "",
            "body": "",
            "date": "2026-08-28T10:00:00+00:00",
            "unread": True,
            "link": "",
            "message_id": f"<m{i}@x>",
            "reply_to": reply_to,
            "uid": str(i),
        },
    )


def make_triaged(i: int, bucket: str = "needs_action") -> Triaged:
    return cast(
        Triaged,
        {
            "bucket": bucket,
            "note": "reply",
            "account": ACCOUNT,
            "sender": "",
            "subject": "",
            "link": "",
            "date": "2026-08-28T10:00:00+00:00",
            "unread": True,
            "idx": i,
            "draft": "",
        },
    )


class _FakeIMAP:
    def __init__(self, host: str, port: int, *, sent: dict[str, list[bytes]] | None = None) -> None:
        self._sent = sent or {}  # TO search term -> raw sent messages, oldest first
        self._by_uid: dict[str, bytes] = {}
        n = 0
        for raws in self._sent.values():
            for raw in raws:
                n += 1
                self._by_uid[str(n)] = raw
        self.select_calls: list[tuple[Any, ...]] = []
        self.uid_calls: list[tuple[str, tuple[Any, ...]]] = []

    def login(self, user: str, pw: str) -> tuple[str, list[bytes]]:
        return "OK", [b"ok"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        return "OK", [b"1"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.uid_calls.append((command, args))
        if command == "SEARCH":
            assert args[1] == "TO"
            term = args[2].strip('"')
            uids = [uid for uid, raw in self._by_uid.items() if raw in self._sent.get(term, [])]
            return "OK", [" ".join(uids).encode()]
        if command == "FETCH":
            assert "PEEK" in args[1]
            out: list[Any] = []
            for uid in args[0].split(","):
                raw = self._by_uid[uid]
                out.append((f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw))
                out.append(b")")
            return "OK", out
        raise AssertionError(f"unexpected uid command {command}")

    def logout(self) -> tuple[str, list[bytes]]:
        return "OK", [b"bye"]

    def list(self, *a: object, **k: object) -> tuple[str, list[bytes]]:
        return "OK", [b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']


class _Factory:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.instances: list[_FakeIMAP] = []

    def __call__(self, host: str, port: int) -> _FakeIMAP:
        inst = _FakeIMAP(host, port, **self.kwargs)  # type: ignore[arg-type]
        self.instances.append(inst)
        return inst


def _patch(monkeypatch: Any, **kwargs: object) -> _Factory:
    factory = _Factory(**kwargs)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", factory)
    return factory


def test_reply_text_keeps_only_words_above_the_quote():
    body = "Hi Bob,\n\nTuesday works.\n\nCheers,\nA\n\nOn Mon, Aug 24, 2026 Bob <bob@x.com> wrote:\n> can we meet?\n"
    assert reply_text(body) == "Hi Bob,\n\nTuesday works.\n\nCheers,\nA"


def test_reply_text_stops_at_signature_separator_and_caps_length():
    assert reply_text("short\n-- \nAlice Example\nCEO") == "short"
    assert len(reply_text("x" * 2000)) == 600


def test_examples_come_from_sent_mail_to_the_recipient_newest_three(monkeypatch: Any) -> None:
    factory = _patch(
        monkeypatch,
        sent={"bob@x.com": [_sent("bob@x.com", f"msg {i}\n\n> quoted") for i in range(5)]},
    )
    emails = [make_email(0, "Bob <Bob@X.com>")]

    examples, warnings = pull_voice_examples(ENV, [make_triaged(0)], emails)

    assert warnings == []
    assert examples == {0: ["msg 2", "msg 3", "msg 4"]}
    fake = factory.instances[0]
    assert fake.select_calls == [('"[Gmail]/Sent Mail"', True)]
    assert [a for c, a in fake.uid_calls if c == "FETCH"] == [("3,4,5", "(BODY.PEEK[]<0.16384>)")]


def test_falls_back_to_domain_then_skips(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch, sent={"@corp.com": [_sent("other@corp.com", "Dear team, noted.")]})
    emails = [make_email(0, "new@corp.com"), make_email(1, "nobody@else.org")]

    examples, _ = pull_voice_examples(ENV, [make_triaged(0), make_triaged(1)], emails)

    assert examples == {0: ["Dear team, noted."]}  # idx 1: no address match, no domain match -> absent
    searches = [a[2] for c, a in factory.instances[0].uid_calls if c == "SEARCH"]
    assert searches == ['"new@corp.com"', '"@corp.com"', '"nobody@else.org"', '"@else.org"']


def test_only_needs_action_items_and_no_connection_when_none(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch)

    examples, warnings = pull_voice_examples(ENV, [make_triaged(0, bucket="worth_reading")], [make_email(0, "b@x")])

    assert examples == {} and warnings == [] and factory.instances == []


def test_missing_password_is_a_warning(monkeypatch: Any) -> None:
    _patch(monkeypatch)

    examples, warnings = pull_voice_examples({}, [make_triaged(0)], [make_email(0, "b@x.com")])

    assert examples == {} and pw_env_var(ACCOUNT) in warnings[0]["error"]
