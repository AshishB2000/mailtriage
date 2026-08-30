"""Pull recent INBOX mail from several Gmail accounts as JSON. Stdlib only.

CRITICAL INVARIANT: the INBOX fetch path (`fetch_account`/`pull`) opens the
mailbox with `select(..., readonly=True)` and `BODY.PEEK[]` and that is
untouched by anything below. `push_drafts` only ever APPENDs a new message to
the account's Drafts mailbox — it never selects INBOX, never sets flags on an
existing message, and nothing in this module ever sends mail.
"""

from __future__ import annotations

import contextlib
import imaplib
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any
from urllib.parse import quote

# Re-exported: tests import MailError from this module too.
from mailtriage.errors import MailError as MailError
from mailtriage.models import Email, PullResult, Triaged


def pw_env_var(addr: str) -> str:
    return "MAIL_PW_" + re.sub(r"[^A-Z0-9]", "_", addr.upper())


def accounts_from_env(environ: Mapping[str, str]) -> list[tuple[str, str]]:
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


def msg_datetime(date_header: str) -> datetime | None:
    try:
        dt = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def within_window(dt: datetime | None, now: datetime, hours: int) -> bool:
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


def snippet_of(msg: EmailMessage, limit: int = 200) -> str:
    part: EmailMessage | None = msg
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
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):  # get_payload(decode=True) is typed loosely; guard the real shape
            payload = b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
    return " ".join(text.split())[:limit]


def parse_message(raw: bytes, addr: str, flags: str, now: datetime, hours: int) -> Email | None:
    msg = message_from_bytes(raw, policy=policy.default)
    dt = msg_datetime(str(msg.get("Date", "")))
    if dt is None or not within_window(dt, now, hours):
        return None
    return {
        "account": addr,
        "from": str(msg.get("From", "")),
        "subject": str(msg.get("Subject", "")),
        "snippet": snippet_of(msg),
        "body": snippet_of(msg, 8000),
        "date": dt.isoformat(),
        "unread": "\\Seen" not in flags,
        "link": gmail_link(addr, str(msg.get("Message-ID", ""))),
        "message_id": str(msg.get("Message-ID", "")),
        "reply_to": str(msg.get("Reply-To", "") or msg.get("From", "")),
    }


def fetch_account(addr: str, pw: str, now: datetime, hours: int, host: str = "imap.gmail.com") -> list[Email]:
    # SINCE is date-granular; go back an extra day, then filter exactly in Python.
    since = (now - timedelta(hours=hours) - timedelta(days=1)).strftime("%d-%b-%Y")
    out: list[Email] = []
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


FetchFn = Callable[[str, str, datetime, int], list[Email]]


def pull(environ: Mapping[str, str], now: datetime, hours: int, fetch: FetchFn = fetch_account) -> PullResult:
    messages: list[Email] = []
    warnings: list[dict[str, str]] = []
    for addr, pw in accounts_from_env(environ):
        try:
            messages.extend(fetch(addr, pw, now, hours))
        except Exception as e:  # imaplib raises many unrelated types — catch broadly, per account
            warnings.append({"account": addr, "error": f"{type(e).__name__}: {e}"})
    messages.sort(key=lambda m: datetime.fromisoformat(m["date"]), reverse=True)
    return {"messages": messages, "warnings": warnings}


def _find_drafts_mailbox(list_lines: list[Any]) -> str:
    """Pick the Drafts mailbox name out of an IMAP LIST response.

    Gmail (and most providers) advertise the RFC 6154 special-use attribute
    (``\\Drafts``) in plain LIST output, so the first line carrying it wins.
    Falls back to Gmail's well-known English name when no line advertises it
    (some accounts/locales omit the attribute).
    """
    for entry in list_lines:
        line = entry[0] if isinstance(entry, tuple) else entry
        if not isinstance(line, bytes) or b"\\Drafts" not in line:
            continue
        m = re.search(rb'"([^"]*)"\s*$', line)
        if m:
            return m.group(1).decode("utf-8", "replace")
        parts = line.split()
        if parts:
            return parts[-1].decode("utf-8", "replace")
    return "[Gmail]/Drafts"


def _quote_mailbox(name: str) -> str:
    # imaplib does not auto-quote names containing "[" or spaces — do it ourselves.
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _build_draft_message(account: str, src: Email, draft: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = account
    msg["To"] = src["reply_to"]
    subject = src["subject"]
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    msg["Subject"] = subject
    message_id = src["message_id"]
    if message_id:
        msg["In-Reply-To"] = message_id
        msg["References"] = message_id
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg.set_content(draft)
    return msg


def push_drafts(
    environ: Mapping[str, str],
    triaged: list[Triaged],
    emails: list[Email],
    host: str = "imap.gmail.com",
) -> list[dict[str, str]]:
    """Append an AI-drafted reply to each needs_action item's account's Drafts
    mailbox, threaded to the original message. Never touches INBOX, never
    sends — see the module docstring's invariant.

    One broken account produces a warning and never aborts the others, same
    philosophy as `pull`: a per-account `except Exception` (imaplib raises
    6+ unrelated types on login/list/append failure).
    """
    by_account: dict[str, list[Triaged]] = {}
    for t in triaged:
        if t["bucket"] == "needs_action" and t["draft"]:
            by_account.setdefault(t["account"], []).append(t)

    warnings: list[dict[str, str]] = []
    for account, items in by_account.items():
        pw = environ.get(pw_env_var(account))
        if not pw:
            warnings.append(
                {"account": account, "error": f"no app password found in ${pw_env_var(account)}, skipping draft push"}
            )
            continue
        try:
            M = imaplib.IMAP4_SSL(host, 993)
            try:
                M.login(account, pw)
                mailbox = _quote_mailbox(_find_drafts_mailbox(M.list()[1] or []))
                for t in items:
                    src = emails[t["idx"]]
                    msg = _build_draft_message(account, src, t["draft"])
                    M.append(mailbox, "\\Draft", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types — one bad account must not abort the rest
            warnings.append({"account": account, "error": f"{type(e).__name__}: {e}"})
    return warnings
