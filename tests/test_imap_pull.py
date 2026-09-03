import imaplib
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from mailtriage.imap_pull import (
    MailError,
    accounts_from_env,
    app_password,
    fetch_account,
    gmail_link,
    legacy_pw_env_var,
    msg_datetime,
    parse_message,
    pull,
    pw_env_var,
    within_window,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

RAW = (
    b"From: Alice <alice@work.com>\r\n"
    b"Subject: Lunch tomorrow?\r\n"
    b"Date: Fri, 28 Aug 2026 09:00:00 +0000\r\n"
    b"Message-ID: <abc123@work.com>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Are you free around noon?   Let me know.\r\n"
)


def test_pw_env_var_hashes_address():
    # BLAKE2b-128 of the trimmed, lower-cased address -- never the address itself.
    assert pw_env_var("alice@gmail.com") == "MAIL_PW_F24FE3C393F64986"
    assert pw_env_var("  Alice@Gmail.com ") == "MAIL_PW_F24FE3C393F64986"
    assert pw_env_var("a.b+x@work.co") == "MAIL_PW_5335BF4B59240EFC"
    assert "ALICE" not in pw_env_var("alice@gmail.com")


def test_legacy_pw_env_var_is_the_old_slug():
    assert legacy_pw_env_var("alice@gmail.com") == "MAIL_PW_ALICE_GMAIL_COM"
    assert legacy_pw_env_var("a.b+x@work.co") == "MAIL_PW_A_B_X_WORK_CO"


def test_app_password_prefers_hashed_name_then_falls_back_to_legacy():
    both = {"MAIL_PW_F24FE3C393F64986": "new", "MAIL_PW_ALICE_GMAIL_COM": "old"}
    assert app_password(both, "alice@gmail.com") == "new"
    assert app_password({"MAIL_PW_ALICE_GMAIL_COM": "old"}, "alice@gmail.com") == "old"
    assert app_password({}, "alice@gmail.com") is None


def test_accounts_from_env_pairs_addresses_with_passwords():
    env = {
        "MAIL_ACCOUNTS": "alice@gmail.com, bob@gmail.com",
        "MAIL_PW_ALICE_GMAIL_COM": "pw1",
        "MAIL_PW_BOB_GMAIL_COM": "pw2",
    }
    assert accounts_from_env(env) == [("alice@gmail.com", "pw1"), ("bob@gmail.com", "pw2")]


def test_accounts_from_env_raises_on_missing_password():
    env = {"MAIL_ACCOUNTS": "alice@gmail.com"}
    with pytest.raises(MailError, match="MAIL_PW_F24FE3C393F64986"):
        accounts_from_env(env)


def test_accounts_from_env_raises_when_unset():
    with pytest.raises(MailError, match="MAIL_ACCOUNTS"):
        accounts_from_env({})


def test_msg_datetime_parses_rfc2822():
    dt = msg_datetime("Fri, 28 Aug 2026 09:00:00 +0000")
    assert dt == datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)


def test_msg_datetime_none_on_garbage():
    assert msg_datetime("") is None
    assert msg_datetime("not a date") is None


def test_within_window_recent_true():
    assert within_window(NOW - timedelta(hours=2), NOW, 13) is True


def test_within_window_too_old_false():
    assert within_window(NOW - timedelta(hours=20), NOW, 13) is False


def test_within_window_future_clamped_false():
    assert within_window(NOW + timedelta(hours=1), NOW, 13) is False


def test_within_window_undated_false():
    assert within_window(None, NOW, 13) is False


def test_gmail_link_uses_rfc822msgid():
    link = gmail_link("me@gmail.com", "<abc123@work.com>")
    assert link == "https://mail.google.com/mail/u/me@gmail.com/#search/rfc822msgid:abc123%40work.com"


def test_parse_message_builds_record():
    rec = parse_message(RAW, "me@gmail.com", "1 (FLAGS () BODY[] {123}", NOW, 13)
    assert rec is not None
    assert rec["from"] == "Alice <alice@work.com>"
    assert rec["subject"] == "Lunch tomorrow?"
    assert rec["snippet"] == "Are you free around noon? Let me know."
    assert rec["body"] == "Are you free around noon? Let me know."
    assert rec["date"] == "2026-08-28T09:00:00+00:00"
    assert rec["unread"] is True
    assert rec["account"] == "me@gmail.com"
    assert "rfc822msgid:abc123" in rec["link"]
    assert rec["message_id"] == "<abc123@work.com>"
    assert rec["reply_to"] == "Alice <alice@work.com>"  # falls back to From when no Reply-To header


def test_parse_message_reply_to_header_wins_over_from():
    raw = RAW.replace(b"Subject: Lunch tomorrow?\r\n", b"Subject: Lunch tomorrow?\r\nReply-To: team@work.com\r\n")
    rec = parse_message(raw, "me@gmail.com", "1 (FLAGS () BODY[]", NOW, 13)
    assert rec is not None
    assert rec["reply_to"] == "team@work.com"


def test_parse_message_seen_flag_marks_read():
    rec = parse_message(RAW, "me@gmail.com", "1 (FLAGS (\\Seen) BODY[] {123}", NOW, 13)
    assert rec is not None
    assert rec["unread"] is False


def test_parse_message_reads_thrid_and_has_no_attachments_for_plain_text():
    rec = parse_message(RAW, "me@gmail.com", "1 (UID 7 X-GM-THRID 1234567 FLAGS () BODY[]", NOW, 13)
    assert rec is not None
    assert rec["thrid"] == "1234567"
    assert rec["attachments"] == []


MULTIPART = (
    b"From: Billing <billing@vendor.com>\r\n"
    b"Subject: Invoice 42\r\n"
    b"Date: Fri, 28 Aug 2026 09:00:00 +0000\r\n"
    b"Message-ID: <inv42@vendor.com>\r\n"
    b'Content-Type: multipart/mixed; boundary="B"\r\n'
    b"\r\n"
    b"--B\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"See attached.\r\n"
    b"--B\r\n"
    b"Content-Type: application/pdf\r\n"
    b'Content-Disposition: attachment; filename="invoice-42.pdf"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"JVBERi0=\r\n"
    b"--B\r\n"
    b"Content-Type: image/png\r\n"
    b'Content-Disposition: inline; filename="logo.png"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"iVBORw0=\r\n"
    b"--B--\r\n"
)


def test_parse_message_lists_attachments_and_named_inline_parts():
    rec = parse_message(MULTIPART, "me@gmail.com", "1 (FLAGS () BODY[]", NOW, 13)
    assert rec is not None
    assert rec["snippet"] == "See attached."
    assert rec["attachments"] == ["invoice-42.pdf (application/pdf)", "logo.png (image/png)"]


def test_parse_message_out_of_window_returns_none():
    old = RAW.replace(b"28 Aug 2026", b"20 Aug 2026")
    assert parse_message(old, "me@gmail.com", "1 (FLAGS () BODY[]", NOW, 13) is None


ENV = {
    "MAIL_ACCOUNTS": "good@gmail.com, bad@gmail.com",
    "MAIL_PW_GOOD_GMAIL_COM": "x",
    "MAIL_PW_BAD_GMAIL_COM": "x",
}


def test_pull_collects_and_sorts_messages():
    def fake_fetch(addr, pw, now, hours, host="imap.gmail.com"):
        return [
            {
                "account": addr,
                "date": "2026-08-28T09:00:00+00:00",
                "from": "a",
                "subject": "s",
                "snippet": "",
                "unread": True,
                "link": "l",
            }
        ]

    out = pull(ENV, NOW, 13, fetch=fake_fetch)
    assert len(out["messages"]) == 2
    assert out["warnings"] == []


def test_pull_records_warning_for_failing_account():
    def fake_fetch(addr, pw, now, hours, host="imap.gmail.com"):
        if addr == "bad@gmail.com":
            raise OSError("login refused")
        return []

    out = pull(ENV, NOW, 13, fetch=fake_fetch)
    assert out["messages"] == []
    assert len(out["warnings"]) == 1
    assert out["warnings"][0]["account"] == "bad@gmail.com"
    assert "login refused" in out["warnings"][0]["error"]


def test_pull_sorts_newest_first():
    def fake_fetch(addr, pw, now, hours, host="imap.gmail.com"):
        stamp = "2026-08-28T11:00:00+00:00" if addr == "good@gmail.com" else "2026-08-28T07:00:00+00:00"
        return [
            {"account": addr, "date": stamp, "from": "a", "subject": "s", "snippet": "", "unread": True, "link": "l"}
        ]

    out = pull(ENV, NOW, 13, fetch=fake_fetch)
    assert out["messages"][0]["account"] == "good@gmail.com"


def test_pull_sorts_by_datetime_not_string():
    """Regression: string sort fails with mixed timezone offsets.
    Message A with +05:30 offset at 20:00 wall-clock (14:30 UTC, older)
    sorts after Message B with +00:00 offset at 15:00 (15:00 UTC, newer).
    Old string sort would rank A first (lexicographically larger).
    """

    def fake_fetch_mixed_tz(addr, pw, now, hours, host="imap.gmail.com"):
        if addr == "old@gmail.com":
            # 2026-08-28 20:00:00 +05:30 = 2026-08-28 14:30:00 UTC (older)
            return [
                {
                    "account": addr,
                    "date": "2026-08-28T20:00:00+05:30",
                    "from": "a",
                    "subject": "older message",
                    "snippet": "",
                    "unread": True,
                    "link": "l",
                }
            ]
        else:  # new@gmail.com
            # 2026-08-28 15:00:00 +00:00 = 2026-08-28 15:00:00 UTC (newer)
            return [
                {
                    "account": addr,
                    "date": "2026-08-28T15:00:00+00:00",
                    "from": "a",
                    "subject": "newer message",
                    "snippet": "",
                    "unread": True,
                    "link": "l",
                }
            ]

    env = {
        "MAIL_ACCOUNTS": "old@gmail.com, new@gmail.com",
        "MAIL_PW_OLD_GMAIL_COM": "x",
        "MAIL_PW_NEW_GMAIL_COM": "x",
    }
    out = pull(env, NOW, 13, fetch=fake_fetch_mixed_tz)
    # The newer message (new@gmail.com, 15:00 UTC) should be first
    assert out["messages"][0]["account"] == "new@gmail.com"
    assert out["messages"][0]["subject"] == "newer message"


class _FakeFetchIMAP:
    """Minimal fake for fetch_account itself (pull()'s other tests inject a
    fake `fetch` function and never touch imaplib at all)."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port

    def login(self, user: str, pw: str) -> tuple[str, list[bytes]]:
        return "OK", [b"Logged in"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        return "OK", [b"1"]

    def search(self, charset: str | None, *criteria: str) -> tuple[str, list[bytes]]:
        return "OK", [b"1"]  # one sequence number

    def fetch(self, num: bytes, spec: str) -> tuple[str, list[Any]]:
        line = f"1 (UID 77 FLAGS () BODY[] {{{len(RAW)}}}".encode()
        return "OK", [(line, RAW), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        return "OK", [b"Logging out"]


def test_fetch_account_populates_uid(monkeypatch: Any) -> None:
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeFetchIMAP)

    out = fetch_account("me@gmail.com", "pw", NOW, 13)

    assert len(out) == 1
    assert out[0]["uid"] == "77"


# --- `only`: a profile's account subset -----------------------------------


def test_accounts_from_env_only_filters_case_insensitively():
    assert [a for a, _ in accounts_from_env(ENV, only={"GOOD@gmail.com"})] == ["good@gmail.com"]


def test_accounts_from_env_only_rejects_an_address_not_in_mail_accounts():
    with pytest.raises(MailError, match="not in MAIL_ACCOUNTS"):
        accounts_from_env(ENV, only={"nope@gmail.com"})


def test_pull_only_fetches_the_named_accounts():
    seen = []

    def fake_fetch(addr, pw, now, hours, host="imap.gmail.com"):
        seen.append(addr)
        return []

    pull(ENV, NOW, 13, fetch=fake_fetch, only={"bad@gmail.com"})
    assert seen == ["bad@gmail.com"]
