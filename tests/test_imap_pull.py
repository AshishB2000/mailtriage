from datetime import datetime, timedelta, timezone

import pytest

from mailtriage.imap_pull import (
    MailError,
    accounts_from_env,
    gmail_link,
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


def test_pw_env_var_slugs_address():
    assert pw_env_var("alice@gmail.com") == "MAIL_PW_ALICE_GMAIL_COM"
    assert pw_env_var("a.b+x@work.co") == "MAIL_PW_A_B_X_WORK_CO"


def test_accounts_from_env_pairs_addresses_with_passwords():
    env = {
        "MAIL_ACCOUNTS": "alice@gmail.com, bob@gmail.com",
        "MAIL_PW_ALICE_GMAIL_COM": "pw1",
        "MAIL_PW_BOB_GMAIL_COM": "pw2",
    }
    assert accounts_from_env(env) == [("alice@gmail.com", "pw1"), ("bob@gmail.com", "pw2")]


def test_accounts_from_env_raises_on_missing_password():
    env = {"MAIL_ACCOUNTS": "alice@gmail.com"}
    with pytest.raises(MailError, match="MAIL_PW_ALICE_GMAIL_COM"):
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
