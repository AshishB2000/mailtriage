"""Pull recent INBOX mail from several Gmail accounts as JSON. Stdlib only."""

import argparse
import contextlib
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from mailtriage.errors import MailError


def pw_env_var(addr: str) -> str:
    return "MAIL_PW_" + re.sub(r"[^A-Z0-9]", "_", addr.upper())


def accounts_from_env(environ) -> list[tuple[str, str]]:
    raw = (environ.get("MAIL_ACCOUNTS") or "").strip()
    if not raw:
        raise MailError("MAIL_ACCOUNTS is empty — set it to a comma-separated list of Gmail addresses.")
    out = []
    for addr in (a.strip() for a in raw.split(",") if a.strip()):
        var = pw_env_var(addr)
        pw = environ.get(var)
        if not pw:
            raise MailError(
                f"{addr}: no app password found in ${var}. Create one at myaccount.google.com/apppasswords."
            )
        out.append((addr, pw))
    return out


def msg_datetime(date_header: str):
    try:
        dt = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def within_window(dt, now, hours) -> bool:
    if dt is None:
        return False
    if dt > now + timedelta(minutes=5):  # future-stamped feeds/senders clamp out
        return False
    return dt >= now - timedelta(hours=hours)


def gmail_link(addr: str, message_id: str) -> str:
    mid = (message_id or "").strip().strip("<>")
    if not mid:
        return f"https://mail.google.com/mail/u/{addr}/#inbox"
    return f"https://mail.google.com/mail/u/{addr}/#search/rfc822msgid:{quote(mid)}"


def snippet_of(msg, limit: int = 200) -> str:
    part = msg
    if msg.is_multipart():
        part = next(
            (
                p
                for p in msg.walk()
                if p.get_content_type() == "text/plain" and "attachment" not in str(p.get("Content-Disposition", ""))
            ),
            None,
        )
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
    return " ".join(text.split())[:limit]


def parse_message(raw: bytes, addr: str, flags: str, now, hours: int):
    msg = message_from_bytes(raw, policy=policy.default)
    dt = msg_datetime(str(msg.get("Date", "")))
    if not within_window(dt, now, hours):
        return None
    return {
        "account": addr,
        "from": str(msg.get("From", "")),
        "subject": str(msg.get("Subject", "")),
        "snippet": snippet_of(msg),
        "date": dt.isoformat(),
        "unread": "\\Seen" not in flags,
        "link": gmail_link(addr, str(msg.get("Message-ID", ""))),
    }


def fetch_account(addr, pw, now, hours, host="imap.gmail.com"):
    # SINCE is date-granular; go back an extra day, then filter exactly in Python.
    since = (now - timedelta(hours=hours) - timedelta(days=1)).strftime("%d-%b-%Y")
    out = []
    M = imaplib.IMAP4_SSL(host, 993)
    try:
        M.login(addr, pw)
        M.select("INBOX", readonly=True)  # readonly => never sets \Seen
        _, data = M.search(None, "SINCE", since)
        for uid in data[0].split():
            _, fetched = M.fetch(uid, "(FLAGS BODY.PEEK[])")  # PEEK => never sets \Seen
            flags, raw = "", b""
            for part in fetched:
                if isinstance(part, tuple):
                    flags = part[0].decode("ascii", "replace")
                    raw = part[1]
            rec = parse_message(raw, addr, flags, now, hours)
            if rec:
                out.append(rec)
    finally:
        with contextlib.suppress(Exception):
            M.logout()
    return out


def pull(environ, now, hours, fetch=fetch_account):
    messages, warnings = [], []
    for addr, pw in accounts_from_env(environ):
        try:
            messages.extend(fetch(addr, pw, now, hours))
        except Exception as e:  # imaplib raises many unrelated types — catch broadly, per account
            warnings.append({"account": addr, "error": f"{type(e).__name__}: {e}"})
    messages.sort(key=lambda m: datetime.fromisoformat(m["date"]), reverse=True)
    return {"messages": messages, "warnings": warnings}


_SELF_CHECK_RAW = (
    b"From: Test <t@example.com>\r\nSubject: hi\r\n"
    b"Date: Fri, 28 Aug 2026 09:00:00 +0000\r\n"
    b"Message-ID: <sc@example.com>\r\n"
    b"Content-Type: text/plain\r\n\r\nbody text\r\n"
)


def _self_check():
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    rec = parse_message(_SELF_CHECK_RAW, "me@gmail.com", "1 (FLAGS () BODY[]", now, 13)
    assert rec is not None and rec["subject"] == "hi", "parser broken"
    assert rec["date"] == "2026-08-28T09:00:00+00:00", "date landmine"
    assert within_window(None, now, 13) is False, "undated must drop"
    assert within_window(now + timedelta(hours=1), now, 13) is False, "future must clamp"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pull recent Gmail INBOX mail as JSON.")
    ap.add_argument("--window-hours", type=int, default=13)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args(argv)
    if args.self_check:
        _self_check()
        print("self-check ok")
        return 0
    now = datetime.now(timezone.utc)
    try:
        result = pull(os.environ, now, args.window_hours)
    except MailError as e:
        print(str(e), file=sys.stderr)
        return 1
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
