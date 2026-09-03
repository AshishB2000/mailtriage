"""Pull recent INBOX mail from several Gmail accounts as JSON. Stdlib only.

CRITICAL INVARIANT: `fetch_account`/`pull`, `pull_open_actions`, and
`pull_week` are read-only -- `select(..., readonly=True)` and `BODY.PEEK[]`
(or `BODY.PEEK[HEADER.FIELDS (...)]`, for `pull_week`) only. The engine
writes exactly two things to Gmail: `label_actions` adds a label to INBOX
messages (the one read-write INBOX `select` in this codebase, required
because STORE on an EXAMINEd mailbox returns NO) and `push_drafts` APPENDs a
new message to the account's Drafts mailbox. Nothing here ever marks a
message read outside of that, sends mail, deletes anything, or moves a
message between mailboxes.
"""

from __future__ import annotations

import contextlib
import hashlib
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
from mailtriage.models import Email, PullResult, Triaged, WeekItem, WeekResult


def pw_env_var(addr: str) -> str:
    """Repo-secret name holding `addr`'s app password.

    MAIL_PW_ + the first 16 hex chars of BLAKE2b-128 over the trimmed,
    lower-cased address -- a hash, so the name (which the public Actions log
    prints) never reveals the address. Mirrored character-for-character by
    `mailPwSlug` in docs/index.html, where libsodium's crypto_generichash is
    the same unkeyed BLAKE2b; tests/test_contracts.py pins both sides to one
    vector.
    """
    digest = hashlib.blake2b(addr.strip().lower().encode(), digest_size=16).hexdigest()
    return "MAIL_PW_" + digest[:16].upper()


def legacy_pw_env_var(addr: str) -> str:
    """The pre-hash name (deprecated): the address itself, upper-cased, with every
    non-alphanumeric turned into `_`. Still *read* so forks set up before the
    change keep working; never written by the wizard any more."""
    return "MAIL_PW_" + re.sub(r"[^A-Z0-9]", "_", addr.upper())


def app_password(environ: Mapping[str, str], addr: str) -> str | None:
    """`addr`'s app password from the hashed secret name, else the legacy one."""
    return environ.get(pw_env_var(addr)) or environ.get(legacy_pw_env_var(addr))


def accounts_from_env(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    raw = (environ.get("MAIL_ACCOUNTS") or "").strip()
    if not raw:
        raise MailError("MAIL_ACCOUNTS is empty — set it to a comma-separated list of Gmail addresses.")
    out = []
    for addr in (a.strip() for a in raw.split(",") if a.strip()):
        pw = app_password(environ, addr)
        if not pw:
            raise MailError(
                f"{addr}: no app password found in ${pw_env_var(addr)}. "
                "Create one at myaccount.google.com/apppasswords."
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


def _older_than_window(dt: datetime | None, now: datetime, hours: int) -> bool:
    """Complement of within_window's lower bound. A carried item must be
    strictly older than the window, or it would duplicate a message the
    normal (dated) `pull` path is about to surface as new."""
    return dt is not None and dt < now - timedelta(hours=hours)


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


def _email_from_msg(msg: EmailMessage, addr: str, flags: str, dt: datetime, uid: str) -> Email:
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
        "uid": uid,
    }


def parse_message(raw: bytes, addr: str, flags: str, now: datetime, hours: int, uid: str = "") -> Email | None:
    msg = message_from_bytes(raw, policy=policy.default)
    dt = msg_datetime(str(msg.get("Date", "")))
    if dt is None or not within_window(dt, now, hours):
        return None
    return _email_from_msg(msg, addr, flags, dt, uid)


def _parse_labeled_message(raw: bytes, addr: str, flags: str, uid: str) -> Email | None:
    """Same parsing as `parse_message`, minus the window filter -- these are
    older by definition; `pull_open_actions` decides what to keep."""
    msg = message_from_bytes(raw, policy=policy.default)
    dt = msg_datetime(str(msg.get("Date", "")))
    if dt is None:  # undated mail is dropped everywhere else too
        return None
    return _email_from_msg(msg, addr, flags, dt, uid)


def fetch_account(addr: str, pw: str, now: datetime, hours: int, host: str = "imap.gmail.com") -> list[Email]:
    # SINCE is date-granular; go back an extra day, then filter exactly in Python.
    since = (now - timedelta(hours=hours) - timedelta(days=1)).strftime("%d-%b-%Y")
    out: list[Email] = []
    M = imaplib.IMAP4_SSL(host, 993)
    try:
        M.login(addr, pw)
        M.select("INBOX", readonly=True)  # readonly => never sets \Seen
        _, data = M.search(None, "SINCE", since)
        for num in data[0].split():
            # UID requested alongside FLAGS so label/draft stages can address this
            # message by UID later without a second round trip to look it up.
            _, fetched = M.fetch(num, "(FLAGS UID BODY.PEEK[])")  # PEEK => never sets \Seen
            flags, raw = "", b""
            for part in fetched:
                if isinstance(part, tuple):
                    flags = part[0].decode("ascii", "replace")
                    raw = part[1]
            rec = parse_message(raw, addr, flags, now, hours, uid=_extract_uid(flags))
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


def _find_mailbox_by_attribute(list_lines: list[Any], attribute: str, fallback: str) -> str:
    """Pick a mailbox name out of an IMAP LIST response by its RFC 6154
    special-use attribute (e.g. ``\\Drafts``, ``\\Sent``).

    Gmail (and most providers) advertise the attribute in plain LIST output,
    so the first line carrying it wins. Falls back to Gmail's well-known
    English name when no line advertises it (some accounts/locales omit it).
    """
    needle = attribute.encode()
    for entry in list_lines:
        line = entry[0] if isinstance(entry, tuple) else entry
        if not isinstance(line, bytes) or needle not in line:
            continue
        m = re.search(rb'"([^"]*)"\s*$', line)
        if m:
            return m.group(1).decode("utf-8", "replace")
        parts = line.split()
        if parts:
            return parts[-1].decode("utf-8", "replace")
    return fallback


def _find_drafts_mailbox(list_lines: list[Any]) -> str:
    return _find_mailbox_by_attribute(list_lines, "\\Drafts", "[Gmail]/Drafts")


def _find_sent_mailbox(list_lines: list[Any]) -> str:
    return _find_mailbox_by_attribute(list_lines, "\\Sent", "[Gmail]/Sent Mail")


def _find_all_mailbox(list_lines: list[Any]) -> str:
    return _find_mailbox_by_attribute(list_lines, "\\All", "[Gmail]/All Mail")


def _quote_mailbox(name: str) -> str:
    # imaplib does not auto-quote strings containing "[", spaces, or "/" --
    # do it ourselves. Used for mailbox names and, identically, for Gmail
    # labels: both are IMAP quoted-strings with the same escaping rules.
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
        pw = app_password(environ, account)
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


def label_actions(
    environ: Mapping[str, str],
    kept: list[Triaged],
    emails: list[Email],
    label: str,
    host: str = "imap.gmail.com",
) -> list[dict[str, str]]:
    """Label every needs_action item on this run's queue (never `carried` --
    those already carry the label from a prior run) so `pull_open_actions`
    can find it again on the next one.

    The ONLY read-write INBOX `select` in this codebase: STORE requires it --
    an EXAMINEd (readonly) mailbox answers STORE with NO. Never FETCH a body
    here; adding a label must not touch \\Seen. Same per-account
    warn-and-continue philosophy as `pull`/`push_drafts`.
    """
    by_account: dict[str, list[str]] = {}
    for t in kept:
        if t["bucket"] != "needs_action":
            continue
        uid = emails[t["idx"]]["uid"]
        if uid:  # no UID (e.g. a synthetic test Email) -- nothing to address, skip it
            by_account.setdefault(t["account"], []).append(uid)

    warnings: list[dict[str, str]] = []
    quoted_label = _quote_mailbox(label)
    for account, uids in by_account.items():
        pw = app_password(environ, account)
        if not pw:
            warnings.append(
                {"account": account, "error": f"no app password found in ${pw_env_var(account)}, skipping labels"}
            )
            continue
        try:
            M = imaplib.IMAP4_SSL(host, 993)
            try:
                M.login(account, pw)
                M.select("INBOX")  # read-write on purpose -- see docstring; STORE needs it
                with contextlib.suppress(Exception):  # whether Gmail auto-creates on STORE is unverified
                    M.create(quoted_label)  # ignore NO/ALREADYEXISTS -- may already exist
                for uid in uids:
                    M.uid("STORE", uid, "+X-GM-LABELS", f"({quoted_label})")
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types — one bad account must not abort the rest
            warnings.append({"account": account, "error": f"{type(e).__name__}: {e}"})
    return warnings


def _extract_uid(flags: str) -> str:
    m = re.search(r"UID (\d+)", flags)
    return m.group(1) if m else ""


def _extract_thrid(flags: str) -> str:
    m = re.search(r"X-GM-THRID (\d+)", flags)
    return m.group(1) if m else ""


def _replied_in_sent(M: imaplib.IMAP4_SSL, thrid: str, message_id: str) -> bool:
    """True when the user's Sent mailbox (must already be the selected
    mailbox) holds a message in the same Gmail thread -- or, when X-GM-THRID
    wasn't available, one that's In-Reply-To the original message."""
    if thrid:
        # None here is the (unquoted) default charset -- correct per RFC and
        # per imaplib's own .search(), whose stub types it as str | None;
        # .uid()'s stub types every arg as plain str, so mypy can't see that.
        _, data = M.uid("SEARCH", None, "X-GM-THRID", thrid)  # type: ignore[arg-type]
        return bool(data and data[0])
    mid = message_id.strip()
    if not mid:
        return False
    _, data = M.uid("SEARCH", None, "HEADER", "In-Reply-To", _quote_mailbox(mid))  # type: ignore[arg-type]
    return bool(data and data[0])


def pull_open_actions(
    environ: Mapping[str, str],
    now: datetime,
    window_hours: int,
    label: str,
    host: str = "imap.gmail.com",
) -> PullResult:
    """Re-surface needs_action mail `label_actions` labeled on a prior run
    and that's still open: still carrying the label, older than the current
    window (an in-window hit is already covered by the normal `pull` path,
    so re-including it here would duplicate it), and with no reply from the
    user anywhere in its Gmail thread. Read-only throughout; makes no model
    call.
    """
    messages: list[Email] = []
    warnings: list[dict[str, str]] = []
    quoted_label = _quote_mailbox(label)
    for addr, pw in accounts_from_env(environ):
        try:
            M = imaplib.IMAP4_SSL(host, 993)
            try:
                M.login(addr, pw)
                M.select("INBOX", readonly=True)
                _, data = M.uid("SEARCH", None, "X-GM-LABELS", quoted_label)  # type: ignore[arg-type]
                uids = data[0].split() if data and data[0] else []

                candidates: list[tuple[Email, str]] = []  # (email, thrid)
                for uid in uids:
                    _, fetched = M.uid("FETCH", uid, "(FLAGS BODY.PEEK[] X-GM-THRID)")
                    flags, raw = "", b""
                    for part in fetched:
                        if isinstance(part, tuple):
                            flags = part[0].decode("ascii", "replace")
                            raw = part[1]
                    rec = _parse_labeled_message(raw, addr, flags, uid.decode())
                    if rec is None:
                        continue
                    if not _older_than_window(datetime.fromisoformat(rec["date"]), now, window_hours):
                        continue  # still in-window -- the normal pull() path already covers it
                    candidates.append((rec, _extract_thrid(flags)))

                if candidates:
                    sent_mailbox = _quote_mailbox(_find_sent_mailbox(M.list()[1] or []))
                    M.select(sent_mailbox, readonly=True)
                    for rec, thrid in candidates:
                        if not _replied_in_sent(M, thrid, rec["message_id"]):
                            messages.append(rec)
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types — one bad account must not abort the rest
            warnings.append({"account": addr, "error": f"{type(e).__name__}: {e}"})
    messages.sort(key=lambda m: datetime.fromisoformat(m["date"]), reverse=True)
    return {"messages": messages, "warnings": warnings}


def _classify_week_item(replied: bool, in_inbox: bool) -> str:
    """Pure classification for pull_week -- replied beats archived beats
    open. Split out from the IMAP calls so self_check can assert on it
    directly, with no network involved."""
    if replied:
        return "replied"
    return "open" if in_inbox else "archived"


def _in_mailbox_by_thrid(M: imaplib.IMAP4_SSL, thrid: str) -> bool:
    """True when the currently selected mailbox holds a message in this
    Gmail thread. Used against INBOX to tell an archived thread from one
    still sitting there. No thrid to search by -> assume still present
    (never call something archived when we can't actually check)."""
    if not thrid:
        return True
    _, data = M.uid("SEARCH", None, "X-GM-THRID", thrid)  # type: ignore[arg-type]
    return bool(data and data[0])


def _parse_week_message(raw: bytes, addr: str, now: datetime, uid: str) -> dict[str, Any] | None:
    """Header-only parse for pull_week -- only FROM/SUBJECT/DATE/MESSAGE-ID
    are ever fetched, never a body. Returns a plain dict (not WeekItem)
    because it also carries `message_id`, needed for the reply lookup but
    not part of the public WeekItem shape; `_to_week_item` projects it down.
    None for undated mail, same as everywhere else in this module."""
    msg = message_from_bytes(raw, policy=policy.default)
    dt = msg_datetime(str(msg.get("Date", "")))
    if dt is None:
        return None
    message_id = str(msg.get("Message-ID", ""))
    return {
        "account": addr,
        "sender": str(msg.get("From", "")),
        "subject": str(msg.get("Subject", "")),
        "date": dt.isoformat(),
        "link": gmail_link(addr, message_id),
        "age_days": max(0, (now - dt).days),
        "message_id": message_id,
    }


def _to_week_item(rec: dict[str, Any]) -> WeekItem:
    return {
        "account": rec["account"],
        "sender": rec["sender"],
        "subject": rec["subject"],
        "date": rec["date"],
        "link": rec["link"],
        "age_days": rec["age_days"],
    }


def pull_week(
    environ: Mapping[str, str],
    now: datetime,
    label: str,
    days: int = 7,
    host: str = "imap.gmail.com",
) -> WeekResult:
    """Weekly roll-up: everything carrying `label` in the last `days` days,
    per account, classified replied / archived / open by pure IMAP
    arithmetic -- no model call, see _classify_week_item.

    Searches Gmail's \\All mailbox (discovered the same attribute-based way
    as \\Sent/\\Drafts), not INBOX, so archived and replied items -- which
    have left INBOX -- are still found. SINCE is date-granular; results are
    re-filtered exactly against `days` in Python, same as fetch_account.
    Only header fields are ever fetched (BODY.PEEK[HEADER.FIELDS ...]), no
    body. Read-only throughout: selects \\All, then (only if there are any
    label hits) \\Sent and INBOX, all `readonly=True`.

    An item whose label was removed since it was actioned is invisible to
    this search -- that's fine, its disappearance from next week's roll-up
    IS the "handled" signal; there is nothing to report for it here.
    """
    since = (now - timedelta(days=days) - timedelta(days=1)).strftime("%d-%b-%Y")
    cutoff = now - timedelta(days=days)
    quoted_label = _quote_mailbox(label)
    accounts: dict[str, dict[str, list[WeekItem]]] = {}
    warnings: list[dict[str, str]] = []

    for addr, pw in accounts_from_env(environ):
        try:
            M = imaplib.IMAP4_SSL(host, 993)
            try:
                M.login(addr, pw)
                list_lines = M.list()[1] or []
                all_mailbox = _quote_mailbox(_find_all_mailbox(list_lines))
                M.select(all_mailbox, readonly=True)
                _, data = M.uid("SEARCH", None, "X-GM-LABELS", quoted_label, "SINCE", since)  # type: ignore[arg-type]
                uids = data[0].split() if data and data[0] else []

                candidates: list[tuple[dict[str, Any], str]] = []  # (rec, thrid)
                for uid in uids:
                    _, fetched = M.uid(
                        "FETCH",
                        uid,
                        "(FLAGS UID X-GM-THRID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])",
                    )
                    flags, raw = "", b""
                    for part in fetched:
                        if isinstance(part, tuple):
                            flags = part[0].decode("ascii", "replace")
                            raw = part[1]
                    rec = _parse_week_message(raw, addr, now, uid.decode())
                    if rec is None or datetime.fromisoformat(rec["date"]) < cutoff:
                        continue
                    candidates.append((rec, _extract_thrid(flags)))

                replied: list[WeekItem] = []
                archived: list[WeekItem] = []
                open_items: list[WeekItem] = []

                if candidates:
                    sent_mailbox = _quote_mailbox(_find_sent_mailbox(list_lines))
                    M.select(sent_mailbox, readonly=True)
                    still_unreplied: list[tuple[dict[str, Any], str]] = []
                    for rec, thrid in candidates:
                        if _replied_in_sent(M, thrid, rec["message_id"]):
                            replied.append(_to_week_item(rec))
                        else:
                            still_unreplied.append((rec, thrid))

                    if still_unreplied:
                        M.select("INBOX", readonly=True)
                        for rec, thrid in still_unreplied:
                            bucket = _classify_week_item(False, _in_mailbox_by_thrid(M, thrid))
                            (open_items if bucket == "open" else archived).append(_to_week_item(rec))

                accounts[addr] = {"replied": replied, "archived": archived, "open": open_items}
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types — one bad account must not abort the rest
            warnings.append({"account": addr, "error": f"{type(e).__name__}: {e}"})

    return {"accounts": accounts, "warnings": warnings}
