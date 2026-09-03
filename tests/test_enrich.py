"""imap_pull.enrich adds read-only context to pulled candidates: earlier
messages of the same Gmail thread. No real network: a fake IMAP4_SSL stands
in, same pattern as tests/test_open_actions.py. Everything here must stay
readonly=True / BODY.PEEK -- see the CRITICAL INVARIANT in imap_pull.
"""

from __future__ import annotations

import imaplib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, cast

from mailtriage.imap_pull import THREAD_CONTEXT_CAP, enrich, pw_env_var
from mailtriage.models import Email

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
ACCOUNT = "alice@gmail.com"
PW_VAR = "MAIL_PW_ALICE_GMAIL_COM"
ENV = {PW_VAR: "pw"}


def _raw(sender: str, hours_ago: float, message_id: str, body: str = "body text") -> bytes:
    dt = NOW - timedelta(hours=hours_ago)
    return (
        f"From: {sender}\r\nSubject: re: thing\r\nDate: {format_datetime(dt)}\r\n"
        f"Message-ID: {message_id}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


def make_email(i: int, **overrides: object) -> Email:
    base: Email = {
        "account": ACCOUNT,
        "from": f"sender{i}@example.com",
        "subject": f"subject {i}",
        "snippet": f"snippet {i}",
        "body": f"body {i}",
        "date": (NOW - timedelta(hours=1)).isoformat(),
        "unread": True,
        "link": f"https://real.example.com/{i}",
        "message_id": f"<real-{i}@example.com>",
        "reply_to": f"sender{i}@example.com",
        "uid": str(100 + i),
        "thrid": str(9000 + i),
    }
    return cast(Email, {**base, **overrides})


class _FakeIMAP:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        threads: dict[str, list[bytes]] | None = None,  # thrid -> uids in \All
        all_messages: dict[bytes, bytes] | None = None,  # \All uid -> raw
        sent_to: dict[str, int] | None = None,  # address -> how many Sent messages go to it
        login_error: Exception | None = None,
    ) -> None:
        self.host, self.port = host, port
        self._threads = threads or {}
        self._all = all_messages or {}
        self._sent_to = sent_to or {}
        self._login_error = login_error
        self.select_calls: list[tuple[Any, ...]] = []
        self.uid_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.store_calls: list[Any] = []

    def login(self, user: str, pw: str) -> tuple[str, list[bytes]]:
        if self._login_error:
            raise self._login_error
        return "OK", [b"Logged in"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        return "OK", [b"1"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.uid_calls.append((command, args))
        cmd = command.upper()
        if cmd == "SEARCH":
            crit = args[1]
            if crit == "X-GM-THRID":
                return "OK", [b" ".join(self._threads.get(args[2], []))]
            if crit == "TO":
                assert args[3] == "SINCE", "sender memory must be bounded by SINCE"
                n = self._sent_to.get(args[2].strip('"'), 0)
                return "OK", [b" ".join(str(i).encode() for i in range(1, n + 1))]
            raise AssertionError(f"unexpected SEARCH criteria: {args}")
        if cmd == "FETCH":
            assert "PEEK" in args[1], "context fetches must use BODY.PEEK, or they mark mail read"
            out: list[Any] = []
            for uid in args[0].split(","):
                raw = self._all[uid.encode()]
                out.append((f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw))
                out.append(b")")
            return "OK", out
        if cmd == "STORE":  # pragma: no cover
            self.store_calls.append(args)
            raise AssertionError("enrich must never STORE")
        raise AssertionError(f"unexpected uid command: {command}")

    def logout(self) -> tuple[str, list[bytes]]:
        return "OK", [b"Logging out"]

    # Defined last: naming a method `list` shadows the builtin for annotations above it.
    def list(self, *a: object, **k: object) -> tuple[str, list[bytes]]:
        return "OK", [
            b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
            b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
        ]


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


# --- thread context -------------------------------------------------------


def test_thread_context_adds_two_earlier_messages_oldest_first(monkeypatch: Any) -> None:
    factory = _patch(
        monkeypatch,
        threads={"9000": [b"1", b"2", b"3", b"4"]},
        all_messages={
            b"1": _raw("Old <old@x.com>", 72, "<m1@x.com>", "way back"),
            b"2": _raw("Bob <bob@x.com>", 30, "<m2@x.com>", "second message here"),
            b"3": _raw("Me <alice@gmail.com>", 5, "<m3@x.com>", "my reply"),
            b"4": _raw("sender0@example.com", 1, "<real-0@example.com>", "the candidate itself"),
        },
    )
    emails = [make_email(0)]

    result = enrich(ENV, emails, NOW, sender_memory=False)

    assert result["warnings"] == []
    assert result["threads"] == 1 and result["fetches"] == 1
    assert emails[0]["thread"] == [
        "1d ago · Bob <bob@x.com>: second message here",
        "5h ago · Me <alice@gmail.com>: my reply",
    ]
    fake = factory.instances[0]
    assert fake.select_calls == [('"[Gmail]/All Mail"', True)]
    fetches = [args for cmd, args in fake.uid_calls if cmd == "FETCH"]
    assert fetches == [("2,3,4", "(BODY.PEEK[]<0.16384>)")]  # one round trip, last 3 uids, bounded bytes


def test_first_message_of_thread_gets_no_context_and_no_fetch(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch, threads={"9000": [b"4"]})
    emails = [make_email(0)]

    result = enrich(ENV, emails, NOW)

    assert "thread" not in emails[0]
    assert result["threads"] == 0 and result["fetches"] == 0
    assert not [1 for cmd, _ in factory.instances[0].uid_calls if cmd == "FETCH"]


def test_thread_context_caps_at_newest_candidates(monkeypatch: Any) -> None:
    n = THREAD_CONTEXT_CAP + 5
    factory = _patch(monkeypatch, threads={str(9000 + i): [b"1"] for i in range(n)})
    emails = [make_email(i) for i in range(n)]  # pull() order: newest first

    enrich(ENV, emails, NOW)

    searched = [args[2] for cmd, args in factory.instances[0].uid_calls if cmd == "SEARCH" and args[1] == "X-GM-THRID"]
    assert searched == [str(9000 + i) for i in range(THREAD_CONTEXT_CAP)]


def test_both_off_never_connects(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch, threads={"9000": [b"1", b"2"]})

    result = enrich(ENV, [make_email(0)], NOW, thread_context=False, sender_memory=False)

    assert factory.instances == []
    assert result == {"threads": 0, "fetches": 0, "senders": 0, "warnings": []}


def test_no_thrid_is_skipped(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch)

    enrich(ENV, [make_email(0, thrid="")], NOW, sender_memory=False)

    assert factory.instances == []


# --- sender memory --------------------------------------------------------


def test_sender_memory_counts_sent_mail_per_distinct_sender(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch, sent_to={"bob@x.com": 3})
    emails = [
        make_email(0, **{"from": "Bob <Bob@X.com>"}),
        make_email(1, **{"from": "bob@x.com"}),  # same address, different casing -- one lookup
        make_email(2, **{"from": "stranger@y.com"}),
    ]

    result = enrich(ENV, emails, NOW, thread_context=False)

    assert result["senders"] == 2
    assert emails[0]["replied_before"] == 3 and emails[1]["replied_before"] == 3
    assert "replied_before" not in emails[2]  # zero is left unset, so build_user prints nothing
    fake = factory.instances[0]
    assert fake.select_calls == [('"[Gmail]/Sent Mail"', True)]
    searches = [args for cmd, args in fake.uid_calls if cmd == "SEARCH"]
    assert searches == [
        (None, "TO", '"bob@x.com"', "SINCE", "01-Mar-2026"),
        (None, "TO", '"stranger@y.com"', "SINCE", "01-Mar-2026"),
    ]


def test_sender_memory_skips_own_address_and_noreply_senders(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch)
    emails = [
        make_email(0, **{"from": f"Me <{ACCOUNT}>"}),
        make_email(1, **{"from": "no-reply@shop.com"}),
        make_email(2, **{"from": "notifications@github.com"}),
        make_email(3, **{"from": "donotreply@bank.com"}),
    ]

    result = enrich(ENV, emails, NOW, thread_context=False)

    assert result["senders"] == 0
    assert factory.instances == []  # nothing to look up -> no connection at all


def test_sender_memory_caps_at_forty_newest_senders(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch)
    emails = [make_email(i, **{"from": f"s{i}@x.com"}) for i in range(45)]

    result = enrich(ENV, emails, NOW, thread_context=False)

    assert result["senders"] == 40
    searched = [args[2] for cmd, args in factory.instances[0].uid_calls if cmd == "SEARCH"]
    assert searched[0] == '"s0@x.com"' and searched[-1] == '"s39@x.com"'


def test_thread_and_sender_lookups_share_one_login(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch, threads={"9000": [b"1"]}, sent_to={"sender0@example.com": 1})

    enrich(ENV, [make_email(0)], NOW)

    assert len(factory.instances) == 1
    assert factory.instances[0].select_calls == [('"[Gmail]/All Mail"', True), ('"[Gmail]/Sent Mail"', True)]


def test_login_failure_is_a_warning_not_a_raise(monkeypatch: Any) -> None:
    _patch(monkeypatch, login_error=OSError("login refused"))

    result = enrich(ENV, [make_email(0)], NOW)

    assert len(result["warnings"]) == 1 and "login refused" in result["warnings"][0]["error"]


def test_missing_password_is_a_warning(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch)

    result = enrich({}, [make_email(0)], NOW)

    assert factory.instances == []
    assert pw_env_var(ACCOUNT) in result["warnings"][0]["error"]
