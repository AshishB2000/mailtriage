"""The capability layer: one fake IMAP server, three servers' worth of
behaviour (Gmail / generic-with-keywords / generic-without), driving every
helper the rest of the codebase is allowed to use.

The Gmail column must match what the pre-D2 engine did exactly -- that is
what tests/test_labels.py, test_open_actions.py, test_week.py and friends
still assert against, with fakes that answer no CAPABILITY at all.
"""

from __future__ import annotations

import imaplib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, cast

import pytest

from mailtriage.commands import label_from_keyword
from mailtriage.imap_pull import (
    Caps,
    accounts_from_env,
    all_mailboxes,
    archive_mailbox,
    archive_message,
    connect,
    create_label,
    detect_caps,
    drafts_mailbox,
    imap_host,
    keyword_for,
    label_actions,
    label_criteria,
    label_noise,
    pull_open_actions,
    search_label,
    select_inbox,
    sent_mailbox,
    smtp_target,
    split_account,
    store_label,
    thread_uids,
    webmail_link,
)
from mailtriage.models import Email

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
LABEL = "mailtriage/action"
GMAIL_CAPS = b"IMAP4rev1 UNSELECT IDLE MOVE X-GM-EXT-1"
KEYWORD_CAPS = b"IMAP4rev1 MOVE SPECIAL-USE LIST-EXTENDED UIDPLUS"
PLAIN_CAPS = b"IMAP4rev1 MOVE SPECIAL-USE"  # no \\* in PERMANENTFLAGS -> folders mode
BARE_CAPS = b"IMAP4rev1 SPECIAL-USE"  # ... and no MOVE either: nothing can carry a label here

GMAIL_LIST = [
    b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
    b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
    b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"',
]
GENERIC_LIST = [
    b'(\\HasNoChildren) "." "INBOX"',
    b'(\\HasNoChildren \\Archive) "." "Archive"',
    b'(\\HasNoChildren \\Sent) "." "Sent"',
    b'(\\HasNoChildren \\Drafts) "." "Drafts"',
]


def _raw(subject: str, hours_ago: float, message_id: str, refs: str = "") -> bytes:
    dt = NOW - timedelta(hours=hours_ago)
    ref_line = f"References: {refs}\r\n" if refs else ""
    return (
        f"From: alice@work.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {format_datetime(dt)}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"{ref_line}"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"body\r\n"
    ).encode()


class _FakeIMAP:
    """One fake, parametrised by `kind`: "gmail", "keywords" or "plain".

    It answers CAPABILITY and PERMANENTFLAGS the way that class of server
    does, and records every command so a test can assert the engine spoke the
    right dialect.
    """

    def __init__(self, host: str, port: int, *, kind: str = "gmail", messages: dict[bytes, bytes] | None = None):
        self.host, self.port, self.kind = host, port, kind
        self._messages = messages or {}
        self.selected = ""
        self.select_calls: list[tuple[str, bool]] = []
        self.uid_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.created: list[str] = []

    def capability(self) -> tuple[str, list[bytes]]:
        return "OK", [{"gmail": GMAIL_CAPS, "keywords": KEYWORD_CAPS, "bare": BARE_CAPS}.get(self.kind, PLAIN_CAPS)]

    def login(self, user: str, pw: str) -> tuple[str, list[bytes]]:
        return "OK", [b"Logged in"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        self.selected = mailbox
        return "OK", [b"3"]

    def response(self, name: str) -> tuple[str, list[bytes]]:
        flags = b"(\\Answered \\Seen $MailtriageAction \\*)" if self.kind == "keywords" else b"(\\Answered \\Seen)"
        return name, [flags if name == "PERMANENTFLAGS" else b""]

    def create(self, name: str) -> tuple[str, list[bytes]]:
        self.created.append(name)
        return "OK", [b"created"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.uid_calls.append((command, args))
        if command.upper() == "SEARCH":
            # Only INBOX holds anything: a \Sent search must come back empty,
            # or every candidate looks like it was already replied to.
            return "OK", [b" ".join(self._messages) if self.selected in ("INBOX", '"INBOX"') else b""]
        if command.upper() == "FETCH":
            out: list[Any] = []
            raw_uids = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            for uid in raw_uids.split(","):
                raw = self._messages[uid.encode()]
                out.append((f"1 (UID {uid} FLAGS () BODY[] {{{len(raw)}}}".encode(), raw))
                out.append(b")")
            return "OK", out
        return "OK", [b"done"]

    def logout(self) -> tuple[str, list[bytes]]:
        return "OK", [b"bye"]

    # Defined last: naming a method `list` shadows the builtin above it.
    def list(self, *a: object, **k: object) -> tuple[str, list[bytes]]:
        return "OK", GMAIL_LIST if self.kind == "gmail" else GENERIC_LIST


class _Factory:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.instances: list[_FakeIMAP] = []

    def __call__(self, host: str, port: int) -> _FakeIMAP:
        inst = _FakeIMAP(host, port, **self.kwargs)  # type: ignore[arg-type]
        self.instances.append(inst)
        return inst


def _patch(monkeypatch: Any, **kwargs: object) -> _Factory:
    f = _Factory(**kwargs)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", f)
    return f


def _caps(monkeypatch: Any, kind: str) -> tuple[imaplib.IMAP4_SSL, Caps]:
    """A logged-in connection to one class of server, INBOX selected -- the
    state every helper below expects. The object is really a _FakeIMAP; `fake`
    gets at what it recorded."""
    _patch(monkeypatch, kind=kind)
    M, caps = connect("me@x.com", "pw", "imap.x.com")
    select_inbox(M, caps)
    return M, caps


def fake(M: imaplib.IMAP4_SSL) -> _FakeIMAP:
    return cast(_FakeIMAP, M)


# --- account specs --------------------------------------------------------


@pytest.mark.parametrize(
    "entry, expected",
    [
        ("alice@gmail.com", ("alice@gmail.com", "imap.gmail.com", "")),
        ("a@fastmail.com|imap.fastmail.com", ("a@fastmail.com", "imap.fastmail.com", "")),
        ("a@work.com | mail.work.com:1993 ", ("a@work.com", "mail.work.com:1993", "")),
        ("a@work.com|mail.work.com|smtp.work.com:587", ("a@work.com", "mail.work.com", "smtp.work.com:587")),
    ],
)
def test_split_account(entry: str, expected: tuple[str, str, str]) -> None:
    assert split_account(entry) == expected


def test_accounts_from_env_keeps_the_address_and_the_secret_name() -> None:
    env = {
        "MAIL_ACCOUNTS": "alice@gmail.com, bob@fastmail.com|imap.fastmail.com",
        "MAIL_PW_F24FE3C393F64986": "pw1",  # pw_env_var("alice@gmail.com") -- unchanged by D2
    }
    from mailtriage.imap_pull import pw_env_var

    # The host half never reaches the hash: the secret is named for the address.
    env[pw_env_var("bob@fastmail.com")] = "pw2"
    assert accounts_from_env(env) == [("alice@gmail.com", "pw1"), ("bob@fastmail.com", "pw2")]
    assert imap_host(env, "alice@gmail.com") == "imap.gmail.com"
    assert imap_host(env, "bob@fastmail.com") == "imap.fastmail.com"


def test_connect_uses_the_entry_host_and_port(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch, kind="keywords")
    connect("me@work.com", "pw", "mail.work.com:1993")
    assert (factory.instances[0].host, factory.instances[0].port) == ("mail.work.com", 1993)


def test_smtp_target_table_and_override() -> None:
    assert smtp_target({"MAIL_ACCOUNTS": "a@gmail.com"}, "a@gmail.com") == ("smtp.gmail.com", 587, False)
    env = {"MAIL_ACCOUNTS": "a@fastmail.com|imap.fastmail.com"}
    assert smtp_target(env, "a@fastmail.com") == ("smtp.fastmail.com", 465, True)
    env = {"MAIL_ACCOUNTS": "a@me.com|imap.mail.me.com"}
    assert smtp_target(env, "a@me.com") == ("smtp.mail.me.com", 587, False)
    env = {"MAIL_ACCOUNTS": "a@w.com|imap.w.com|smtp.w.com:587"}
    assert smtp_target(env, "a@w.com") == ("smtp.w.com", 587, False)
    # Unknown host, no third field: imap.<domain> -> smtp.<domain> on 465.
    assert smtp_target({"MAIL_ACCOUNTS": "a@w.com|imap.w.com"}, "a@w.com") == ("smtp.w.com", 465, True)


# --- capability detection -------------------------------------------------


def test_detect_caps_per_server_class(monkeypatch: Any) -> None:
    _, gmail = _caps(monkeypatch, "gmail")
    assert (gmail.mode, gmail.gmail, gmail.move) == ("gmail", True, True)

    _, kw = _caps(monkeypatch, "keywords")
    assert (kw.mode, kw.gmail, kw.keywords, kw.special_use, kw.move) == ("keywords", False, True, True, True)

    _, plain = _caps(monkeypatch, "plain")
    assert (plain.mode, plain.keywords, plain.move) == ("folders", False, True)


def test_a_server_that_answers_no_capability_is_trusted_by_host_name() -> None:
    class _Mute:
        def capability(self) -> tuple[str, list[bytes]]:
            raise OSError("no CAPABILITY here")

    mute = cast(imaplib.IMAP4_SSL, _Mute())
    assert detect_caps(mute, "imap.gmail.com").gmail is True, "every existing Gmail fake depends on this"
    assert detect_caps(mute, "imap.fastmail.com").gmail is False


def test_doctor_summary_is_counts_and_booleans_only(monkeypatch: Any) -> None:
    _, caps = _caps(monkeypatch, "keywords")
    assert caps.summary() == "keywords mode · keywords=yes special-use=yes move=yes"


# --- labels ---------------------------------------------------------------


def test_keyword_names_are_atoms_and_round_trip() -> None:
    assert keyword_for(LABEL) == "$MailtriageAction"
    assert keyword_for("mailtriage/until-2026-09-10") == "$MailtriageUntil20260910"
    assert keyword_for("mailtriage/snooze-1w") == "$MailtriageSnooze1w"
    for label in ("mailtriage/until-2026-09-10", "mailtriage/snooze-3d"):
        assert label_from_keyword(keyword_for(label)) == label
    assert all(c not in keyword_for("mailtriage/until-2026-09-10") for c in "/- ")


def test_label_search_and_store_speak_the_right_dialect(monkeypatch: Any) -> None:
    M, caps = _caps(monkeypatch, "gmail")
    search_label(M, caps, LABEL, "SINCE", "01-Sep-2026")
    store_label(M, caps, b"7", LABEL)
    rec = fake(M)
    assert rec.uid_calls[-2] == ("SEARCH", (None, "X-GM-LABELS", '"mailtriage/action"', "SINCE", "01-Sep-2026"))
    assert rec.uid_calls[-1] == ("STORE", ("7", "+X-GM-LABELS", '("mailtriage/action")'))

    M, caps = _caps(monkeypatch, "keywords")
    search_label(M, caps, LABEL, "SINCE", "01-Sep-2026")
    store_label(M, caps, b"7", LABEL, add=False)
    rec = fake(M)
    assert rec.uid_calls[-2] == ("SEARCH", (None, "KEYWORD", "$MailtriageAction", "SINCE", "01-Sep-2026"))
    assert rec.uid_calls[-1] == ("STORE", ("7", "-FLAGS", "($MailtriageAction)"))
    assert label_criteria(caps, LABEL) == ["KEYWORD", "$MailtriageAction"]


def test_folders_mode_moves_instead_of_flagging_and_searches_the_folder(monkeypatch: Any) -> None:
    M, caps = _caps(monkeypatch, "plain")
    assert store_label(M, caps, b"7", LABEL) is True
    rec = fake(M)
    assert rec.uid_calls[-1] == ("MOVE", ("7", '"mailtriage/action"'))
    search_label(M, caps, LABEL)
    assert rec.select_calls[-1] == ('"mailtriage/action"', True)
    # Removing it puts the message back where the reader can see it.
    store_label(M, caps, b"7", LABEL, add=False)
    assert rec.uid_calls[-1] == ("MOVE", ("7", '"INBOX"'))


def test_a_server_with_no_labels_no_keywords_and_no_move_is_told_so(monkeypatch: Any) -> None:
    M, caps = _caps(monkeypatch, "bare")
    assert (caps.mode, caps.move) == ("folders", False)
    assert store_label(M, caps, b"7", LABEL) is False, "the caller must be able to warn instead of crashing"


def test_create_label_is_a_mailbox_only_where_labels_are_mailboxes(monkeypatch: Any) -> None:
    for kind, expected in (("gmail", ['"mailtriage/action"']), ("keywords", []), ("plain", ['"mailtriage/action"'])):
        M, caps = _caps(monkeypatch, kind)
        create_label(M, caps, LABEL)
        assert fake(M).created == expected, kind


def test_label_actions_warns_once_when_the_server_cannot_carry_over(monkeypatch: Any) -> None:
    from tests.test_labels import PW_VAR, make_email, make_triaged

    _patch(monkeypatch, kind="bare")
    warnings = label_actions({PW_VAR: "pw"}, [make_triaged(0), make_triaged(1)], [make_email(0), make_email(1)], LABEL)
    assert len(warnings) == 1 and "carry-over" in warnings[0]["error"]


# --- mailboxes, archiving, threads, links ---------------------------------


def test_special_use_mailboxes_per_server(monkeypatch: Any) -> None:
    M, caps = _caps(monkeypatch, "gmail")
    assert (sent_mailbox(M, caps), drafts_mailbox(M, caps)) == ("[Gmail]/Sent Mail", "[Gmail]/Drafts")
    assert all_mailboxes(M, caps) == ["[Gmail]/All Mail"]

    M, caps = _caps(monkeypatch, "keywords")
    assert (sent_mailbox(M, caps), drafts_mailbox(M, caps)) == ("Sent", "Drafts")
    assert archive_mailbox(M, caps) == "Archive"
    assert all_mailboxes(M, caps) == ["INBOX", "Archive"], "\\All stands in as INBOX + \\Archive"


def test_archive_message_drops_the_inbox_label_or_moves(monkeypatch: Any) -> None:
    M, caps = _caps(monkeypatch, "gmail")
    assert archive_message(M, caps, b"5") is True
    assert fake(M).uid_calls[-1] == ("STORE", ("5", "-X-GM-LABELS", "(\\Inbox)"))

    M, caps = _caps(monkeypatch, "keywords")
    assert archive_message(M, caps, b"5") is True
    assert fake(M).uid_calls[-1] == ("MOVE", ("5", '"Archive"'))

    M, caps = _caps(monkeypatch, "keywords")
    caps.move = False
    assert archive_message(M, caps, b"5") is False, "no MOVE -> the caller counts a warning, nothing is deleted"


def test_label_noise_warns_once_when_it_cannot_archive(monkeypatch: Any) -> None:
    from tests.test_labels import PW_VAR, make_email

    class _NoMove(_FakeIMAP):
        def capability(self) -> tuple[str, list[bytes]]:
            return "OK", [b"IMAP4rev1 SPECIAL-USE"]  # keywords come from PERMANENTFLAGS; no MOVE

    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, port: _NoMove(host, port, kind="keywords"))
    touched, warnings = label_noise({PW_VAR: "pw"}, [make_email(0), make_email(1)], [0, 1], archive=True)
    assert touched == 2, "the label still goes on -- only the archiving is skipped"
    assert len(warnings) == 1 and "nothing was deleted" in warnings[0]["error"]


def test_thread_uids_falls_back_to_the_references_chain(monkeypatch: Any) -> None:
    em = cast(Email, {"thrid": "999", "refs": ["<a@x.com>", "<b@x.com>"], "message_id": "<c@x.com>"})
    M, caps = _caps(monkeypatch, "gmail")
    thread_uids(M, caps, em)
    assert fake(M).uid_calls[-1] == ("SEARCH", (None, "X-GM-THRID", "999"))

    M, caps = _caps(monkeypatch, "keywords")
    thread_uids(M, caps, em)
    searches = [args for cmd, args in fake(M).uid_calls if cmd == "SEARCH"]
    assert searches == [
        (None, "HEADER", "Message-ID", '"<a@x.com>"'),
        (None, "HEADER", "Message-ID", '"<b@x.com>"'),
    ], "no thread ids: ask for the message's own ancestors by Message-ID"


def test_webmail_link_per_provider() -> None:
    assert webmail_link("imap.gmail.com", "me@gmail.com", "<a@b.com>") == (
        "https://mail.google.com/mail/u/me@gmail.com/#search/rfc822msgid:a%40b.com"
    )
    assert webmail_link("imap.fastmail.com", "me@fastmail.com", "<a@b.com>") == (
        "https://app.fastmail.com/mail/search:msgid%3Aa%40b.com"
    )
    assert webmail_link("imap.mail.me.com", "me@icloud.com", "<a@b.com>") == "https://www.icloud.com/mail"
    assert webmail_link("mail.work.com", "me@work.com", "<a@b.com>") == "message:a%40b.com"
    assert webmail_link("mail.work.com", "me@work.com", "") == "", "no Message-ID, no link -- never a dead one"


# --- an end-to-end read on a generic server -------------------------------


def test_pull_open_actions_on_a_keyword_server(monkeypatch: Any) -> None:
    from mailtriage.imap_pull import pw_env_var

    env = {"MAIL_ACCOUNTS": "me@fastmail.com|imap.fastmail.com", pw_env_var("me@fastmail.com"): "pw"}
    factory = _patch(monkeypatch, kind="keywords", messages={b"11": _raw("still open", 40, "<open@work.com>")})

    result = pull_open_actions(env, NOW, 15, LABEL)

    assert [m["subject"] for m in result["messages"]] == ["still open"]
    conn = factory.instances[0]
    assert (conn.host, conn.port) == ("imap.fastmail.com", 993)
    assert ("SEARCH", (None, "KEYWORD", "$MailtriageAction")) in conn.uid_calls
    assert conn.select_calls == [("INBOX", True), ('"Sent"', True)]
    assert result["messages"][0]["link"].startswith("https://app.fastmail.com/mail/search:msgid")
