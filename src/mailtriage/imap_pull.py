"""Pull recent INBOX mail from several Gmail accounts as JSON. Stdlib only.

CRITICAL INVARIANT: `fetch_account`/`pull`, `pull_open_actions`, `pull_week`,
`already_delivered`, and `check_login` are read-only -- `select(..., readonly=True)` and `BODY.PEEK[]`
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
from email.utils import format_datetime, parseaddr, parsedate_to_datetime
from typing import Any
from urllib.parse import quote

# Re-exported: tests import MailError from this module too.
from mailtriage.errors import MailError as MailError
from mailtriage.models import Email, EnrichResult, PullResult, Triaged, WeekItem, WeekResult


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


def accounts_from_env(environ: Mapping[str, str], only: set[str] | None = None) -> list[tuple[str, str]]:
    """(address, app password) for every MAIL_ACCOUNTS entry -- or, with
    `only`, just those addresses (a profile's `accounts`), which must all be
    listed in MAIL_ACCOUNTS."""
    raw = (environ.get("MAIL_ACCOUNTS") or "").strip()
    if not raw:
        raise MailError("MAIL_ACCOUNTS is empty — set it to a comma-separated list of Gmail addresses.")
    addrs = [a.strip() for a in raw.split(",") if a.strip()]
    if only is not None:
        wanted = {a.strip().lower() for a in only}
        missing = sorted(wanted - {a.lower() for a in addrs})
        if missing:
            raise MailError(
                f"{', '.join(missing)}: not in MAIL_ACCOUNTS. A profile's `accounts` in config.yaml may only "
                "name addresses that are in the MAIL_ACCOUNTS secret."
            )
        addrs = [a for a in addrs if a.lower() in wanted]
    out = []
    for addr in addrs:
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


def plain_text(msg: EmailMessage) -> str:
    """The first non-attachment text/plain part, decoded, line breaks intact."""
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
    return str(text)


def snippet_of(msg: EmailMessage, limit: int = 200) -> str:
    return " ".join(plain_text(msg).split())[:limit]


_QUOTE_START_RE = re.compile(r"^(>|On .{0,200}wrote:\s*$|-{2,}\s*$|_{5,}|From: .*$|-----Original Message-----)")


def reply_text(body: str, limit: int = 600) -> str:
    """The reader's own words from a sent message: everything above the first
    quoted line, signature separator, or forwarded-header block, trimmed."""
    # ponytail: a Gmail "On <date>, <name> wrote:" line that wrapped onto two
    # lines slips through; the 600-char cap keeps the damage to a few words.
    kept: list[str] = []
    for line in body.splitlines():
        if _QUOTE_START_RE.match(line.strip()):
            break
        kept.append(line.rstrip())
    return "\n".join(kept).strip()[:limit]


def attachments_of(msg: EmailMessage, limit: int = 10) -> list[str]:
    """'name (content/type)' for every part that is an attachment or carries a
    filename (inline images with a name count; unnamed body parts don't)."""
    out: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        if not name and "attachment" not in str(part.get("Content-Disposition", "")).lower():
            continue
        out.append(f"{name or 'unnamed'} ({part.get_content_type()})")
        if len(out) == limit:
            break
    return out


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
        "thrid": _extract_thrid(flags),
        "attachments": attachments_of(msg),
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
            _, fetched = M.fetch(num, "(FLAGS UID X-GM-THRID BODY.PEEK[])")  # PEEK => never sets \Seen
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


def pull(
    environ: Mapping[str, str],
    now: datetime,
    hours: int,
    fetch: FetchFn = fetch_account,
    only: set[str] | None = None,
) -> PullResult:
    messages: list[Email] = []
    warnings: list[dict[str, str]] = []
    for addr, pw in accounts_from_env(environ, only):
        try:
            messages.extend(fetch(addr, pw, now, hours))
        except Exception as e:  # imaplib raises many unrelated types — catch broadly, per account
            warnings.append({"account": addr, "error": f"{type(e).__name__}: {e}"})
    messages.sort(key=lambda m: datetime.fromisoformat(m["date"]), reverse=True)
    return {"messages": messages, "warnings": warnings}


def check_login(environ: Mapping[str, str], host: str = "imap.gmail.com") -> list[tuple[str, int, str]]:
    """`mailtriage --doctor`'s account check: (addr, INBOX message count,
    error) per MAIL_ACCOUNTS account, error == "" on success. Login + a
    readonly SELECT only -- nothing is fetched."""
    out: list[tuple[str, int, str]] = []
    for addr, pw in accounts_from_env(environ):
        try:
            M = imaplib.IMAP4_SSL(host, 993)
            try:
                M.login(addr, pw)
                _, data = M.select("INBOX", readonly=True)
                out.append((addr, int(data[0] or b"0"), ""))
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types — report, don't abort the other accounts
            out.append((addr, 0, f"{type(e).__name__}: {e}"))
    return out


def already_delivered(
    environ: Mapping[str, str], subject_prefix: str, stamp: str, now: datetime, host: str = "imap.gmail.com"
) -> bool:
    """The no-double-send guard: True when any MAIL_ACCOUNTS mailbox already
    holds a message since yesterday whose subject contains
    "<subject_prefix> · <stamp>" (e.g. "mailtriage · Thu 03 Sep 08:00").
    Gmail is the memory -- there is no state file, by design.

    Searches the \\All mailbox (LIST special-use, like pull_week) so a
    digest sent to yourself is found whether it sits in INBOX or Sent;
    falls back to INBOX + \\Sent when \\All can't be selected. The IMAP
    SEARCH is by the ASCII slot stamp only (imaplib sends commands as
    ASCII), then each hit's Subject header is checked for the full string in
    Python, so a calendar invite carrying the same time can't suppress a
    real digest. Read-only; best-effort: a dead account never vetoes a send.
    """
    since = (now - timedelta(days=1)).strftime("%d-%b-%Y")
    subject = f"{subject_prefix} · {stamp}"
    for addr, pw in accounts_from_env(environ):
        # imaplib raises many unrelated types — a dead account can't veto the send
        with contextlib.suppress(Exception):
            M = imaplib.IMAP4_SSL(host, 993)
            try:
                M.login(addr, pw)
                list_lines = M.list()[1] or []
                boxes = [_find_all_mailbox(list_lines), "INBOX", _find_sent_mailbox(list_lines)]
                for i, box in enumerate(boxes):
                    if M.select(_quote_mailbox(box), readonly=True)[0] != "OK":
                        continue
                    _, data = M.uid("SEARCH", None, "SUBJECT", _quote_mailbox(stamp), "SINCE", since)  # type: ignore[arg-type]
                    for uid in data[0].split() if data and data[0] else []:
                        _, fetched = M.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
                        raw = next((part[1] for part in fetched if isinstance(part, tuple)), b"")
                        if subject in str(message_from_bytes(raw, policy=policy.default).get("Subject", "")):
                            return True
                    if i == 0:
                        break  # \All holds INBOX and Sent too -- no need to search them again
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
    return False


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


VOICE_EXAMPLES = 3  # most recent Sent messages per recipient shown to the drafting model


def _last_uids(M: imaplib.IMAP4_SSL, *criteria: str) -> list[bytes]:
    _, data = M.uid("SEARCH", None, *criteria)  # type: ignore[arg-type]
    uids = data[0].split() if data and data[0] else []
    return uids[-VOICE_EXAMPLES:]


def pull_voice_examples(
    environ: Mapping[str, str],
    triaged: list[Triaged],
    emails: list[Email],
    host: str = "imap.gmail.com",
) -> tuple[dict[int, list[str]], list[dict[str, str]]]:
    """For each needs_action item, up to VOICE_EXAMPLES of the reader's own
    recent Sent messages to the same recipient (falling back to the same
    domain), as reply text only -- nothing below a quoted section. Keyed by
    the item's idx. Read-only: \\Sent is selected readonly and fetched with
    BODY.PEEK, bounded to one SEARCH (two on domain fallback) plus one FETCH
    per distinct recipient. Same per-account warn-and-continue as `pull`.
    """
    by_account: dict[str, list[Triaged]] = {}
    for t in triaged:
        if t["bucket"] == "needs_action":
            by_account.setdefault(t["account"], []).append(t)

    examples: dict[int, list[str]] = {}
    warnings: list[dict[str, str]] = []
    for account, items in by_account.items():
        pw = environ.get(pw_env_var(account))
        if not pw:
            warnings.append(
                {"account": account, "error": f"no app password found in ${pw_env_var(account)}, skipping voice"}
            )
            continue
        try:
            M = imaplib.IMAP4_SSL(host, 993)
            try:
                M.login(account, pw)
                M.select(_quote_mailbox(_find_sent_mailbox(M.list()[1] or [])), readonly=True)
                cache: dict[str, list[str]] = {}
                for t in items:
                    addr = parseaddr(emails[t["idx"]]["reply_to"])[1].lower()
                    if not addr or "@" not in addr:
                        continue
                    if addr not in cache:
                        uids = _last_uids(M, "TO", _quote_mailbox(addr)) or _last_uids(
                            M, "TO", _quote_mailbox("@" + addr.rsplit("@", 1)[1])
                        )
                        texts = [
                            reply_text(plain_text(message_from_bytes(raw, policy=policy.default)))
                            for _flags, raw in _fetch_many(M, uids, f"(BODY.PEEK[]{_PARTIAL})")
                        ]
                        cache[addr] = [s for s in texts if s]
                    if cache[addr]:
                        examples[t["idx"]] = cache[addr]
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types — one bad account must not abort the rest
            warnings.append({"account": account, "error": f"{type(e).__name__}: {e}"})
    return examples, warnings


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


THREAD_CONTEXT_CAP = 15  # candidates per run that get earlier-thread context -- the newest ones
THREAD_PREV = 2  # earlier messages shown per thread
# Bounded partial fetch for context messages: headers plus the start of the
# body is all a 300-char snippet needs, and it keeps a 40-candidate run's
# extra bytes small no matter how big the thread's attachments are.
_PARTIAL = "<0.16384>"


def _age(sent: datetime, now: datetime) -> str:
    # Same shape as the age in triage.build_user, kept local so this module
    # never imports the triage package.
    minutes = max(0, int((now - sent).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 24 * 60:
        return f"{minutes // 60}h ago"
    return f"{minutes // (24 * 60)}d ago"


def _fetch_many(M: imaplib.IMAP4_SSL, uids: list[bytes], spec: str) -> list[tuple[str, bytes]]:
    """One UID FETCH for several messages -> (flags line, raw) per message."""
    if not uids:
        return []
    _, fetched = M.uid("FETCH", b",".join(uids).decode(), spec)
    return [(p[0].decode("ascii", "replace"), p[1]) for p in fetched if isinstance(p, tuple)]


def _thread_lines(M: imaplib.IMAP4_SSL, em: Email, now: datetime) -> tuple[list[str], int]:
    """Up to THREAD_PREV messages that came before `em` in its Gmail thread,
    oldest first, read from the currently selected \\All mailbox. Returns
    (lines, extra fetches made)."""
    _, data = M.uid("SEARCH", None, "X-GM-THRID", em["thrid"])  # type: ignore[arg-type]
    uids = data[0].split() if data and data[0] else []
    if len(uids) < 2:
        return [], 0  # first (or only) message of its thread -- nothing earlier to show
    sent_at = datetime.fromisoformat(em["date"])
    earlier: list[tuple[datetime, str]] = []
    # ponytail: the last THREAD_PREV+1 UIDs are the thread's newest arrivals,
    # which normally means the candidate plus what came just before it. A
    # thread that grew again after the candidate shows fewer earlier lines.
    for _flags, raw in _fetch_many(M, uids[-(THREAD_PREV + 1) :], f"(BODY.PEEK[]{_PARTIAL})"):
        msg = message_from_bytes(raw, policy=policy.default)
        dt = msg_datetime(str(msg.get("Date", "")))
        if dt is None or dt >= sent_at or str(msg.get("Message-ID", "")) == em["message_id"]:
            continue
        earlier.append((dt, f"{_age(dt, now)} · {msg.get('From', '')}: {snippet_of(msg, 300)}"))
    earlier.sort(key=lambda pair: pair[0])
    return [line for _, line in earlier[-THREAD_PREV:]], 1


SENDER_MEMORY_CAP = 40  # distinct sender addresses looked up in \Sent per run
SENDER_MEMORY_DAYS = 180
# Automated senders nobody replies to -- a Sent search for them is a wasted round trip.
_NOREPLY_RE = re.compile(r"no-?reply|do-?not-?reply|notifications?@|mailer-daemon|bounce|^postmaster@", re.IGNORECASE)


def _sender_addresses(items: list[Email], own: set[str], budget: int) -> list[str]:
    """Distinct candidate sender addresses worth a \\Sent lookup, in the order
    given (newest candidate first), skipping the reader's own accounts and
    noreply-ish senders."""
    out: list[str] = []
    for em in items:
        if len(out) >= budget:
            break
        addr = parseaddr(em["from"])[1].lower()
        if not addr or addr in own or addr in out or _NOREPLY_RE.search(addr):
            continue
        out.append(addr)
    return out


def _sent_count(M: imaplib.IMAP4_SSL, addr: str, since: str) -> int:
    """Messages in the selected \\Sent mailbox addressed to `addr` since `since`."""
    _, data = M.uid("SEARCH", None, "TO", _quote_mailbox(addr), "SINCE", since)  # type: ignore[arg-type]
    return len(data[0].split()) if data and data[0] else 0


def enrich(
    environ: Mapping[str, str],
    emails: list[Email],
    now: datetime,
    *,
    thread_context: bool = True,
    sender_memory: bool = True,
    host: str = "imap.gmail.com",
) -> EnrichResult:
    """Fill the optional context keys on `emails` in place, after `pull`:
    `thread` (earlier messages of the same Gmail thread, from \\All) and
    `replied_before` (how often the reader has written to that sender, from
    \\Sent).

    Read-only throughout (`readonly=True`, `BODY.PEEK`), one login per
    account that has something to look up, and the same warn-and-continue
    shape as `pull`. Round trips are bounded by the caps above, and the
    counts returned are what `cli` prints -- never the content.
    """
    threads = fetches = senders = 0
    warnings: list[dict[str, str]] = []
    by_account: dict[str, list[Email]] = {}
    for em in emails:  # pull() sorted newest first, so the caps below favor the newest
        by_account.setdefault(em["account"], []).append(em)
    own = {a.lower() for a in by_account}
    since = (now - timedelta(days=SENDER_MEMORY_DAYS)).strftime("%d-%b-%Y")

    thread_budget = THREAD_CONTEXT_CAP if thread_context else 0
    sender_budget = SENDER_MEMORY_CAP if sender_memory else 0
    for account, items in by_account.items():
        want_threads = [em for em in items if em.get("thrid")][:thread_budget]
        want_senders = _sender_addresses(items, own, sender_budget)
        if not want_threads and not want_senders:
            continue
        pw = environ.get(pw_env_var(account))
        if not pw:
            warnings.append(
                {"account": account, "error": f"no app password found in ${pw_env_var(account)}, skipping context"}
            )
            continue
        try:
            M = imaplib.IMAP4_SSL(host, 993)
            try:
                M.login(account, pw)
                list_lines = M.list()[1] or []
                if want_threads:
                    M.select(_quote_mailbox(_find_all_mailbox(list_lines)), readonly=True)
                    for em in want_threads:
                        lines, n = _thread_lines(M, em, now)
                        fetches += n
                        thread_budget -= 1
                        if lines:
                            em["thread"] = lines
                            threads += 1
                if want_senders:
                    M.select(_quote_mailbox(_find_sent_mailbox(list_lines)), readonly=True)
                    counts = {addr: _sent_count(M, addr, since) for addr in want_senders}
                    senders += len(counts)
                    sender_budget -= len(counts)
                    for em in items:
                        n = counts.get(parseaddr(em["from"])[1].lower(), 0)
                        if n:
                            em["replied_before"] = n
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types — one bad account must not abort the rest
            warnings.append({"account": account, "error": f"{type(e).__name__}: {e}"})
    return {"threads": threads, "fetches": fetches, "senders": senders, "warnings": warnings}


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
    only: set[str] | None = None,
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
    for addr, pw in accounts_from_env(environ, only):
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
    only: set[str] | None = None,
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

    for addr, pw in accounts_from_env(environ, only):
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
