"""Pull recent INBOX mail from several Gmail accounts as JSON. Stdlib only."""

import contextlib
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from mailtriage.errors import MailError
from mailtriage.models import Email, PullResult


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


def parse_message(raw: bytes, addr: str, flags: str, now, hours: int) -> Email | None:
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


def pull(environ, now, hours, fetch=fetch_account) -> PullResult:
    messages, warnings = [], []
    for addr, pw in accounts_from_env(environ):
        try:
            messages.extend(fetch(addr, pw, now, hours))
        except Exception as e:  # imaplib raises many unrelated types — catch broadly, per account
            warnings.append({"account": addr, "error": f"{type(e).__name__}: {e}"})
    messages.sort(key=lambda m: datetime.fromisoformat(m["date"]), reverse=True)
    return {"messages": messages, "warnings": warnings}
