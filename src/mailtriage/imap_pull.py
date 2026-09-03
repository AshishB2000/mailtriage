r"""Pull recent INBOX mail from several IMAP accounts as JSON. Stdlib only.

CRITICAL INVARIANT: `fetch_account`/`pull`, `pull_open_actions`, `pull_week`,
`already_delivered`, and `check_login` are read-only -- `select(..., readonly=True)` and `BODY.PEEK[]`
(or `BODY.PEEK[HEADER.FIELDS (...)]`, for `pull_week`) only. The engine
writes exactly three things to the mailbox: `label_actions` and `label_noise`
add a label to INBOX messages (the read-write INBOX `select`s in this
codebase, required because STORE on an EXAMINEd mailbox returns NO) --
`label_noise` with `archive=True` is the ONE opt-in exception that takes a
message out of the inbox (Gmail: drop the `\Inbox` label; elsewhere: MOVE to
`\Archive`) -- and `push_drafts` APPENDs a new message to the account's
Drafts mailbox. Nothing here ever marks a message read outside of that, sends
mail, or deletes/EXPUNGEs anything.

CAPABILITY LAYER: everything Gmail-specific lives behind the helpers in the
"capabilities" section below (`search_label`, `store_label`, `thread_uids`,
`archive_message`, `all_mailboxes`, `webmail_link`, ...). No caller -- here,
in commands.py, or anywhere else -- writes `X-GM-*` itself: a server without
`X-GM-EXT-1` gets IMAP keywords, special-use folders and header searches
instead, decided once per connection in `Caps`.
"""

from __future__ import annotations

import contextlib
import hashlib
import imaplib
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr, parsedate_to_datetime
from typing import Any
from urllib.parse import quote

# Re-exported: tests import MailError from this module too.
from mailtriage.errors import MailError as MailError
from mailtriage.models import Email, EnrichResult, PullResult, Triaged, WeekItem, WeekResult

# Header stamped on every draft push_drafts appends, and the one thing
# count_drafts searches for -- the weekly "time saved" line counts drafts
# mailtriage wrote, never one the reader wrote themselves.
DRAFT_MARKER = "X-Mailtriage"


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


GMAIL_IMAP = "imap.gmail.com"
IMAP_PORT = 993

# IMAP host -> (SMTP host, port, implicit TLS) for `delivery: mailbox`. Gmail
# keeps the 587+STARTTLS pair `delivery: gmail` has always used; an entry that
# names its own SMTP host (`addr|imap.host|smtp.host[:port]`) wins over this.
SMTP_HOSTS: dict[str, tuple[str, int, bool]] = {
    GMAIL_IMAP: ("smtp.gmail.com", 587, False),
    "imap.fastmail.com": ("smtp.fastmail.com", 465, True),
    "imap.mail.me.com": ("smtp.mail.me.com", 587, False),
}


def split_account(entry: str) -> tuple[str, str, str]:
    """One MAIL_ACCOUNTS entry -> (address, imap host[:port], smtp host[:port]).

    `alice@gmail.com` (Gmail implied), `alice@fastmail.com|imap.fastmail.com`,
    `…|imap.host:1993`, or `…|imap.host|smtp.host:587`. Always TLS -- there is
    no plaintext form, so a port is just a port. The address alone is what the
    MAIL_PW_ secret is hashed from, whichever form is used.
    """
    parts = [p.strip() for p in entry.split("|")]
    addr = parts[0]
    host = parts[1] if len(parts) > 1 and parts[1] else GMAIL_IMAP
    smtp = parts[2] if len(parts) > 2 and parts[2] else ""
    return addr, host, smtp


def _entries(environ: Mapping[str, str]) -> list[str]:
    raw = (environ.get("MAIL_ACCOUNTS") or "").strip()
    if not raw:
        raise MailError(
            "MAIL_ACCOUNTS is empty — set it to a comma-separated list of addresses "
            "(alice@gmail.com, or alice@fastmail.com|imap.fastmail.com for a non-Gmail mailbox)."
        )
    return [a.strip() for a in raw.split(",") if a.strip()]


def imap_host(environ: Mapping[str, str], addr: str) -> str:
    """`addr`'s IMAP host[:port] from MAIL_ACCOUNTS; Gmail when the entry
    names none. Never raises -- the stages that write (drafts, labels) are
    handed accounts by name and must not care whether MAIL_ACCOUNTS is
    readable from here."""
    with contextlib.suppress(MailError):
        for entry in _entries(environ):
            a, host, _smtp = split_account(entry)
            if a.lower() == addr.strip().lower():
                return host
    return GMAIL_IMAP


def smtp_target(environ: Mapping[str, str], addr: str) -> tuple[str, int, bool]:
    """(host, port, implicit TLS) to send `addr`'s own mail through: the
    entry's third field when it has one, else the IMAP host's table entry,
    else the IMAP host with `imap.` swapped for `smtp.` on 465."""
    host = GMAIL_IMAP
    smtp = ""
    with contextlib.suppress(MailError):
        for entry in _entries(environ):
            a, host_i, smtp_i = split_account(entry)
            if a.lower() == addr.strip().lower():
                host, smtp = host_i, smtp_i
                break
    if smtp:
        name, _, port = smtp.partition(":")
        n = int(port) if port.isdigit() else 465
        return name, n, n == 465
    name = host.partition(":")[0]
    if name in SMTP_HOSTS:
        return SMTP_HOSTS[name]
    return re.sub(r"^imap[.-]", "smtp.", name), 465, True


def accounts_from_env(environ: Mapping[str, str], only: set[str] | None = None) -> list[tuple[str, str]]:
    """(address, app password) for every MAIL_ACCOUNTS entry -- or, with
    `only`, just those addresses (a profile's `accounts`), which must all be
    listed in MAIL_ACCOUNTS. The host half of an entry is dropped here; ask
    `imap_host`/`smtp_target` for it."""
    addrs = [split_account(e)[0] for e in _entries(environ)]
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
                "Gmail: myaccount.google.com/apppasswords. Fastmail: Settings -> Privacy & Security -> "
                "app passwords. iCloud: appleid.apple.com -> Sign-In and Security -> App-Specific Passwords."
            )
        out.append((addr, pw))
    return out


# --- capabilities: the ONE place that knows what a server can do ----------


@dataclass(slots=True)
class Caps:
    """What this connection's server supports, read once at login.

    `gmail` is `X-GM-EXT-1` (labels, thread ids, All Mail). Without it the
    engine falls back, in order: IMAP keywords (`$MailtriageAction`) when
    INBOX's PERMANENTFLAGS advertise `\\*`, else a `mailtriage/action` folder
    reachable with `MOVE`. `boxes` caches this connection's LIST response --
    mailbox discovery is per-connection, never per-call.
    """

    gmail: bool = True
    keywords: bool = True
    special_use: bool = True
    move: bool = False
    boxes: list[Any] = field(default_factory=list)

    @property
    def mode(self) -> str:
        """ "gmail" | "keywords" | "folders" -- how a label is stored here."""
        return "gmail" if self.gmail else "keywords" if self.keywords else "folders"

    def summary(self) -> str:
        """One line for `--doctor`: the mode and the booleans, never a name."""
        flags = " ".join(
            f"{k}={'yes' if v else 'no'}"
            for k, v in (("keywords", self.keywords), ("special-use", self.special_use), ("move", self.move))
        )
        return f"{self.mode} mode · {flags}"


def detect_caps(M: imaplib.IMAP4_SSL, host: str) -> Caps:
    """Read CAPABILITY. A server that won't answer it is trusted to be what
    its host name says -- which is why every existing Gmail path (and every
    Gmail test fake) keeps behaving exactly as before."""
    raw = ""
    with contextlib.suppress(Exception):  # imaplib raises several types; an unanswered CAPABILITY is not fatal
        raw = " ".join(x.decode("ascii", "replace") for x in (M.capability()[1] or []) if x).upper()
    if not raw:
        return Caps(gmail=host.partition(":")[0] == GMAIL_IMAP)
    gmail = "X-GM-EXT-1" in raw
    return Caps(
        gmail=gmail,
        # Generic servers only earn this after INBOX's PERMANENTFLAGS say so.
        keywords=gmail,
        special_use=gmail or "SPECIAL-USE" in raw or "LIST-EXTENDED" in raw,
        move=gmail or "MOVE" in raw,
    )


def connect(addr: str, pw: str, host: str) -> tuple[imaplib.IMAP4_SSL, Caps]:
    """Log in to `host` ("name" or "name:port", always TLS) and detect what
    it can do. Every IMAP connection in this codebase opens here."""
    name, _, port = host.partition(":")
    M = imaplib.IMAP4_SSL(name, int(port) if port.isdigit() else IMAP_PORT)
    try:
        M.login(addr, pw)
    except Exception:
        with contextlib.suppress(Exception):
            M.logout()
        raise
    return M, detect_caps(M, host)


def select_inbox(M: imaplib.IMAP4_SSL, caps: Caps, readonly: bool = True) -> Any:
    """SELECT INBOX and, on a generic server, learn from PERMANENTFLAGS
    whether this mailbox takes custom keywords (`\\*`)."""
    status = M.select("INBOX", readonly=readonly)
    if not caps.gmail:
        with contextlib.suppress(Exception):  # not every server (or fake) answers .response()
            caps.keywords = any(b"\\*" in line for line in (M.response("PERMANENTFLAGS")[1] or []) if line)
    return status


def keyword_for(label: str) -> str:
    """A label as an IMAP keyword: 'mailtriage/action' -> '$MailtriageAction',
    'mailtriage/until-2026-09-10' -> '$MailtriageUntil20260910'. Keywords are
    atoms -- no '/', no '-', no spaces -- so every run of non-alphanumerics
    becomes a word boundary instead."""
    return "$" + "".join(p[:1].upper() + p[1:] for p in re.split(r"[^0-9A-Za-z]+", label) if p)


def label_criteria(caps: Caps, label: str) -> list[str]:
    """The SEARCH terms matching `label` on this server. Usable inside a
    bigger criteria list (including after NOT), which is why it is a list."""
    if caps.gmail:
        return ["X-GM-LABELS", _quote_mailbox(label)]
    return ["KEYWORD", keyword_for(label)]


def search_label(M: imaplib.IMAP4_SSL, caps: Caps, label: str, *extra: str) -> list[bytes]:
    """UIDs carrying `label`, plus any extra criteria. In folders mode the
    label IS a mailbox, so this selects it read-only and searches there --
    the caller's own selection is replaced."""
    if caps.mode == "folders":
        if M.select(_quote_mailbox(label), readonly=True)[0] != "OK":
            return []
        criteria = list(extra) or ["ALL"]
    else:
        criteria = [*label_criteria(caps, label), *extra]
    # None here is the (unquoted) default charset -- correct per RFC and per
    # imaplib's own .search(), whose stub types it as str | None; .uid()'s
    # stub types every arg as plain str, so mypy can't see that.
    _, data = M.uid("SEARCH", None, *criteria)  # type: ignore[arg-type]
    return data[0].split() if data and data[0] else []


def store_label(M: imaplib.IMAP4_SSL, caps: Caps, uid: bytes | str, label: str, add: bool = True) -> bool:
    """Add or remove `label` on `uid` in the selected (read-write) mailbox:
    a Gmail label, an IMAP keyword, or -- with neither -- a MOVE into (out
    of) the label's own folder. False when the server can do none of the
    three; the caller counts that as one warning, never a crash."""
    uid_s = uid.decode() if isinstance(uid, bytes) else uid
    if caps.gmail:
        M.uid("STORE", uid_s, f"{'+' if add else '-'}X-GM-LABELS", f"({_quote_mailbox(label)})")
    elif caps.keywords:
        M.uid("STORE", uid_s, f"{'+' if add else '-'}FLAGS", f"({keyword_for(label)})")
    elif caps.move:
        # COPY + \Deleted is forbidden here -- this engine never sets \Deleted.
        M.uid("MOVE", uid_s, _quote_mailbox(label if add else "INBOX"))
    else:
        return False
    return True


def create_label(M: imaplib.IMAP4_SSL, caps: Caps, label: str) -> None:
    """Make sure `label` exists, where that means anything: a Gmail label and
    a fallback folder are mailboxes, an IMAP keyword is not."""
    if caps.keywords and not caps.gmail:
        return
    with contextlib.suppress(Exception):  # NO/ALREADYEXISTS is the common case
        M.create(_quote_mailbox(label))


def _list_lines(M: imaplib.IMAP4_SSL, caps: Caps) -> list[Any]:
    if not caps.boxes:
        caps.boxes = M.list()[1] or []
    return caps.boxes


def sent_mailbox(M: imaplib.IMAP4_SSL, caps: Caps) -> str:
    return _find_mailbox_by_attribute(_list_lines(M, caps), "\\Sent", "[Gmail]/Sent Mail" if caps.gmail else "Sent")


def drafts_mailbox(M: imaplib.IMAP4_SSL, caps: Caps) -> str:
    return _find_mailbox_by_attribute(_list_lines(M, caps), "\\Drafts", "[Gmail]/Drafts" if caps.gmail else "Drafts")


def archive_mailbox(M: imaplib.IMAP4_SSL, caps: Caps) -> str:
    return _find_mailbox_by_attribute(_list_lines(M, caps), "\\Archive", "Archive")


def all_mailboxes(M: imaplib.IMAP4_SSL, caps: Caps) -> list[str]:
    """Where "everything, filed or not" lives: Gmail's one \\All mailbox, or
    INBOX plus the \\Archive folder. Callers that collect messages from these
    must de-duplicate by Message-ID -- a message can sit in both."""
    if caps.gmail:
        return [_find_mailbox_by_attribute(_list_lines(M, caps), "\\All", "[Gmail]/All Mail")]
    archive = archive_mailbox(M, caps)
    return ["INBOX", archive] if archive else ["INBOX"]


def archive_message(M: imaplib.IMAP4_SSL, caps: Caps, uid: bytes | str) -> bool:
    """Take a message out of the inbox without deleting it: Gmail drops the
    \\Inbox label, everyone else MOVEs it to \\Archive. False when the server
    offers neither -- the caller counts it and keeps triaging."""
    uid_s = uid.decode() if isinstance(uid, bytes) else uid
    if caps.gmail:
        M.uid("STORE", uid_s, "-X-GM-LABELS", "(\\Inbox)")
        return True
    archive = archive_mailbox(M, caps) if caps.special_use else ""
    if caps.move and archive:
        M.uid("MOVE", uid_s, _quote_mailbox(archive))
        return True
    return False


def thread_uids(M: imaplib.IMAP4_SSL, caps: Caps, em: Email) -> list[bytes]:
    """UIDs of `em`'s conversation in the selected mailbox: Gmail's thread id
    when the server has one, else one `HEADER Message-ID` search per entry in
    the message's own References/In-Reply-To chain."""
    if caps.gmail and em.get("thrid"):
        _, data = M.uid("SEARCH", None, "X-GM-THRID", em["thrid"])  # type: ignore[arg-type]
        return data[0].split() if data and data[0] else []
    out: list[bytes] = []
    for mid in em.get("refs", [])[-THREAD_PREV:]:
        _, data = M.uid("SEARCH", None, "HEADER", "Message-ID", _quote_mailbox(mid))  # type: ignore[arg-type]
        out += data[0].split() if data and data[0] else []
    return out


def same_thread_present(M: imaplib.IMAP4_SSL, caps: Caps, thrid: str, message_id: str) -> bool:
    """True when the selected mailbox holds anything from this conversation.
    Gmail asks by thread id; elsewhere it is the message itself, by
    Message-ID. Nothing to ask by -> True, so nothing is ever called archived
    on a guess."""
    if caps.gmail and thrid:
        _, data = M.uid("SEARCH", None, "X-GM-THRID", thrid)  # type: ignore[arg-type]
        return bool(data and data[0])
    mid = (message_id or "").strip()
    if not mid:
        return True
    _, data = M.uid("SEARCH", None, "HEADER", "Message-ID", _quote_mailbox(mid))  # type: ignore[arg-type]
    return bool(data and data[0])


def webmail_link(host: str, addr: str, message_id: str) -> str:
    """A link that opens the message in the account's own web client. Known
    providers get a real deep link; anything else gets the `message:` URI
    some desktop clients handle, or "" when there is no Message-ID."""
    mid = (message_id or "").strip().strip("<>")
    name = host.partition(":")[0]
    if name == GMAIL_IMAP:
        return gmail_link(addr, message_id)
    if not mid:
        return {
            "imap.fastmail.com": "https://app.fastmail.com/mail/Inbox",
            "imap.mail.me.com": "https://www.icloud.com/mail",
        }.get(name, "")
    if name == "imap.fastmail.com":
        return f"https://app.fastmail.com/mail/search:msgid%3A{quote(mid)}"
    if name == "imap.mail.me.com":
        return "https://www.icloud.com/mail"
    return f"message:{quote(mid)}"


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


def unsubscribe_of(header: str) -> str:
    """The https URL from a List-Unsubscribe header, else its mailto:, else
    "". Nothing else -- an http: or javascript: entry never becomes a link."""
    targets = [t.strip() for t in re.findall(r"<([^>]+)>", header or "")]
    for scheme in ("https://", "mailto:"):
        for t in targets:
            if t.lower().startswith(scheme):
                return str(t)
    return ""


def refs_of(msg: EmailMessage) -> list[str]:
    """Message-IDs this message replies to, oldest first: References plus
    In-Reply-To. The generic-server stand-in for Gmail's thread id."""
    raw = f"{msg.get('References', '')} {msg.get('In-Reply-To', '')}"
    seen: list[str] = []
    for mid in re.findall(r"<[^<>\s]+>", str(raw)):
        if mid not in seen:
            seen.append(mid)
    return seen


def _email_from_msg(msg: EmailMessage, addr: str, flags: str, dt: datetime, uid: str, host: str = GMAIL_IMAP) -> Email:
    return {
        "account": addr,
        "from": str(msg.get("From", "")),
        "subject": str(msg.get("Subject", "")),
        "snippet": snippet_of(msg),
        "body": snippet_of(msg, 8000),
        "date": dt.isoformat(),
        "unread": "\\Seen" not in flags,
        "link": webmail_link(host, addr, str(msg.get("Message-ID", ""))),
        "message_id": str(msg.get("Message-ID", "")),
        "reply_to": str(msg.get("Reply-To", "") or msg.get("From", "")),
        "uid": uid,
        "thrid": _extract_thrid(flags),
        "refs": refs_of(msg),
        "attachments": attachments_of(msg),
        "unsubscribe": unsubscribe_of(str(msg.get("List-Unsubscribe", ""))),
    }


def parse_message(
    raw: bytes, addr: str, flags: str, now: datetime, hours: int, uid: str = "", host: str = GMAIL_IMAP
) -> Email | None:
    msg = message_from_bytes(raw, policy=policy.default)
    dt = msg_datetime(str(msg.get("Date", "")))
    if dt is None or not within_window(dt, now, hours):
        return None
    return _email_from_msg(msg, addr, flags, dt, uid, host)


def _parse_labeled_message(raw: bytes, addr: str, flags: str, uid: str, host: str = GMAIL_IMAP) -> Email | None:
    """Same parsing as `parse_message`, minus the window filter -- these are
    older by definition; `pull_open_actions` decides what to keep."""
    msg = message_from_bytes(raw, policy=policy.default)
    dt = msg_datetime(str(msg.get("Date", "")))
    if dt is None:  # undated mail is dropped everywhere else too
        return None
    return _email_from_msg(msg, addr, flags, dt, uid, host)


def fetch_account(addr: str, pw: str, now: datetime, hours: int, host: str = GMAIL_IMAP) -> list[Email]:
    # SINCE is date-granular; go back an extra day, then filter exactly in Python.
    since = (now - timedelta(hours=hours) - timedelta(days=1)).strftime("%d-%b-%Y")
    out: list[Email] = []
    M, caps = connect(addr, pw, host)
    try:
        select_inbox(M, caps)  # readonly => never sets \Seen
        _, data = M.search(None, "SINCE", since)
        for num in data[0].split():
            # UID requested alongside FLAGS so label/draft stages can address this
            # message by UID later without a second round trip to look it up.
            spec = "(FLAGS UID X-GM-THRID BODY.PEEK[])" if caps.gmail else "(FLAGS UID BODY.PEEK[])"
            _, fetched = M.fetch(num, spec)  # PEEK => never sets \Seen
            flags, raw = "", b""
            for part in fetched:
                if isinstance(part, tuple):
                    flags = part[0].decode("ascii", "replace")
                    raw = part[1]
            rec = parse_message(raw, addr, flags, now, hours, uid=_extract_uid(flags), host=host)
            if rec:
                out.append(rec)
    finally:
        with contextlib.suppress(Exception):
            M.logout()
    return out


FetchFn = Callable[[str, str, datetime, int, str], list[Email]]


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
            messages.extend(fetch(addr, pw, now, hours, imap_host(environ, addr)))
        except Exception as e:  # imaplib raises many unrelated types — catch broadly, per account
            warnings.append({"account": addr, "error": f"{type(e).__name__}: {e}"})
    messages.sort(key=lambda m: datetime.fromisoformat(m["date"]), reverse=True)
    return {"messages": messages, "warnings": warnings}


def check_login(environ: Mapping[str, str], host: str = "") -> list[tuple[str, int, str, str]]:
    """`mailtriage --doctor`'s account check: (addr, INBOX message count,
    error, capability summary) per MAIL_ACCOUNTS account, error == "" on
    success. Login + a readonly SELECT only -- nothing is fetched. The
    summary is `Caps.summary()`: the mode and the booleans, no mailbox
    names."""
    out: list[tuple[str, int, str, str]] = []
    for addr, pw in accounts_from_env(environ):
        try:
            M, caps = connect(addr, pw, host or imap_host(environ, addr))
            try:
                _, data = select_inbox(M, caps)
                out.append((addr, int(data[0] or b"0"), "", caps.summary()))
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types — report, don't abort the other accounts
            out.append((addr, 0, f"{type(e).__name__}: {e}", ""))
    return out


def already_delivered(
    environ: Mapping[str, str], subject_prefix: str, stamp: str, now: datetime, host: str = ""
) -> bool:
    """The no-double-send guard: True when any MAIL_ACCOUNTS mailbox already
    holds a message since yesterday whose subject contains
    "<subject_prefix> · <stamp>" (e.g. "mailtriage · Thu 03 Sep 08:00").
    Gmail is the memory -- there is no state file, by design.

    Searches `all_mailboxes` (Gmail's \\All; elsewhere INBOX + \\Archive) so
    a digest sent to yourself is found wherever it landed, then INBOX and
    \\Sent as a fallback when the first mailbox can't be selected. The IMAP
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
            M, caps = connect(addr, pw, host or imap_host(environ, addr))
            try:
                boxes = [*all_mailboxes(M, caps), "INBOX", sent_mailbox(M, caps)]
                for i, box in enumerate(boxes):
                    if M.select(_quote_mailbox(box), readonly=True)[0] != "OK":
                        continue
                    _, data = M.uid("SEARCH", None, "SUBJECT", _quote_mailbox(stamp), "SINCE", since)  # type: ignore[arg-type]
                    for uid in data[0].split() if data and data[0] else []:
                        _, fetched = M.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
                        raw = next((part[1] for part in fetched if isinstance(part, tuple)), b"")
                        if subject in str(message_from_bytes(raw, policy=policy.default).get("Subject", "")):
                            return True
                    if i == 0 and caps.gmail:
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


def _quote_mailbox(name: str) -> str:
    # imaplib does not auto-quote strings containing "[", spaces, or "/" --
    # do it ourselves. Used for mailbox names and, identically, for Gmail
    # labels: both are IMAP quoted-strings with the same escaping rules.
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _build_draft_message(account: str, src: Email, draft: str, suffix: str = "") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = account
    msg["To"] = src["reply_to"]
    subject = src["subject"]
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    msg["Subject"] = subject + suffix
    # The marker count_drafts searches for. A header, not a subject heuristic:
    # the reader can rewrite a draft's subject entirely and it still counts,
    # and a draft they wrote themselves never does.
    msg[DRAFT_MARKER] = "draft"
    message_id = src["message_id"]
    if message_id:
        msg["In-Reply-To"] = message_id
        msg["References"] = message_id
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg.set_content(draft)
    return msg


def count_drafts(environ: Mapping[str, str], now: datetime, days: int = 7, host: str = "imap.gmail.com") -> int:
    """How many drafts mailtriage pushed in the last `days` days, across every
    account's \\Drafts mailbox -- found by the `DRAFT_MARKER` header
    `push_drafts` stamps, so a draft the reader wrote themselves never counts.
    Search only, read-only, nothing fetched. Never raises: this is one
    cosmetic line of the weekly review, and an unset MAIL_ACCOUNTS or a dead
    account contributes 0 rather than costing the reader their review."""
    since = (now - timedelta(days=days)).strftime("%d-%b-%Y")
    total = 0
    try:
        accounts = accounts_from_env(environ)
    except MailError:
        return 0
    for addr, pw in accounts:
        with contextlib.suppress(Exception):  # imaplib raises many unrelated types; a count never fails a run
            M = imaplib.IMAP4_SSL(host, 993)
            try:
                M.login(addr, pw)
                M.select(_quote_mailbox(_find_drafts_mailbox(M.list()[1] or [])), readonly=True)
                # None = default charset, same imaplib stub quirk as _replied_in_sent.
                _, data = M.uid("SEARCH", None, "HEADER", DRAFT_MARKER, "draft", "SINCE", since)  # type: ignore[arg-type]
                total += len(data[0].split() if data and data[0] else [])
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
    return total


def push_drafts(
    environ: Mapping[str, str],
    triaged: list[Triaged],
    emails: list[Email],
    host: str = "",
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
            M, caps = connect(account, pw, host or imap_host(environ, account))
            try:
                mailbox = _quote_mailbox(drafts_mailbox(M, caps))
                for t in items:
                    src = emails[t["idx"]]
                    full = t.get("draft_full", "")
                    # Two variants -> two separate threaded drafts, told apart by subject.
                    variants = [(t["draft"], " [A short]"), (full, " [B full]")] if full else [(t["draft"], "")]
                    for draft, suffix in variants:
                        msg = _build_draft_message(account, src, draft, suffix)
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
    host: str = "",
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
        pw = app_password(environ, account)
        if not pw:
            warnings.append(
                {"account": account, "error": f"no app password found in ${pw_env_var(account)}, skipping voice"}
            )
            continue
        try:
            M, caps = connect(account, pw, host or imap_host(environ, account))
            try:
                M.select(_quote_mailbox(sent_mailbox(M, caps)), readonly=True)
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
    host: str = "",
) -> list[dict[str, str]]:
    """Label every needs_action item on this run's queue (never `carried` --
    those already carry the label from a prior run) so `pull_open_actions`
    can find it again on the next one.

    A read-write INBOX `select`: STORE requires it -- an EXAMINEd (readonly)
    mailbox answers STORE with NO. Never FETCH a body here; adding a label
    must not touch \\Seen. What "label" means is `store_label`'s problem
    (Gmail label / IMAP keyword / MOVE into a folder); a server that can do
    none of the three gets one warning and the run carries on without
    carry-over. Same per-account warn-and-continue as `pull`/`push_drafts`.
    """
    by_account: dict[str, list[str]] = {}
    for t in kept:
        if t["bucket"] != "needs_action":
            continue
        uid = emails[t["idx"]]["uid"]
        if uid:  # no UID (e.g. a synthetic test Email) -- nothing to address, skip it
            by_account.setdefault(t["account"], []).append(uid)

    warnings: list[dict[str, str]] = []
    for account, uids in by_account.items():
        pw = app_password(environ, account)
        if not pw:
            warnings.append(
                {"account": account, "error": f"no app password found in ${pw_env_var(account)}, skipping labels"}
            )
            continue
        try:
            M, caps = connect(account, pw, host or imap_host(environ, account))
            try:
                select_inbox(M, caps, readonly=False)  # read-write on purpose -- see docstring; STORE needs it
                create_label(M, caps, label)  # ignore NO/ALREADYEXISTS -- may already exist
                for uid in uids:
                    if not store_label(M, caps, uid, label):
                        warnings.append({"account": account, "error": UNLABELABLE})
                        break
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types — one bad account must not abort the rest
            warnings.append({"account": account, "error": f"{type(e).__name__}: {e}"})
    return warnings


NOISE_LABEL = "mailtriage/noise"
UNLABELABLE = (
    "this server has neither Gmail labels, IMAP keywords (PERMANENTFLAGS has no \\*), nor MOVE — "
    "carry-over and label commands are unavailable here; triage still runs"
)
UNARCHIVABLE = "this server has no MOVE or no \\Archive folder — noise stays in the inbox; nothing was deleted"


def label_noise(
    environ: Mapping[str, str],
    emails: list[Email],
    idxs: list[int],
    archive: bool = False,
    host: str = "",
) -> tuple[int, list[dict[str, str]]]:
    """Opt-in (config `noise.label`): apply NOISE_LABEL to the candidates at
    `idxs` -- the caller passes `rules.omitted`, so a rule-protected sender
    never reaches here. With `archive` (config `noise.archive`),
    `archive_message` also takes them out of the inbox (Gmail: the `\\Inbox`
    label comes off; elsewhere: MOVE to `\\Archive`) -- they stay findable
    and are never deleted or expunged. Same read-write INBOX select and
    per-account warn-and-continue as `label_actions`; never fetches a body.
    Returns (messages touched, warnings)."""
    by_account: dict[str, list[str]] = {}
    for i in idxs:
        em = emails[i]
        if em["uid"]:
            by_account.setdefault(em["account"], []).append(em["uid"])

    touched = 0
    warnings: list[dict[str, str]] = []
    for account, uids in by_account.items():
        pw = app_password(environ, account)
        if not pw:
            warnings.append(
                {"account": account, "error": f"no app password found in ${pw_env_var(account)}, skipping noise labels"}
            )
            continue
        try:
            M, caps = connect(account, pw, host or imap_host(environ, account))
            try:
                select_inbox(M, caps, readonly=False)  # read-write on purpose -- STORE needs it
                create_label(M, caps, NOISE_LABEL)
                warned = False
                for uid in uids:
                    if not store_label(M, caps, uid, NOISE_LABEL):
                        warnings.append({"account": account, "error": UNLABELABLE})
                        break
                    # In folders mode the label write was itself a MOVE out of
                    # INBOX -- the message is already out; don't move it twice.
                    if archive and caps.mode != "folders" and not archive_message(M, caps, uid) and not warned:
                        warnings.append({"account": account, "error": UNARCHIVABLE})
                        warned = True
                    touched += 1
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types — one bad account must not abort the rest
            warnings.append({"account": account, "error": f"{type(e).__name__}: {e}"})
    return touched, warnings


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


def _thread_lines(M: imaplib.IMAP4_SSL, caps: Caps, em: Email, now: datetime) -> tuple[list[str], int]:
    """Up to THREAD_PREV messages that came before `em` in its conversation,
    oldest first, read from the currently selected mailbox. `thread_uids`
    decides how the conversation is found (Gmail thread id, or the
    References chain). Returns (lines, extra fetches made)."""
    uids = thread_uids(M, caps, em)
    # Gmail's search returns the candidate itself too; a References search
    # returns only the ancestors, so one hit there is already worth a fetch.
    if len(uids) < (2 if caps.gmail else 1):
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
    host: str = "",
) -> EnrichResult:
    """Fill the optional context keys on `emails` in place, after `pull`:
    `thread` (earlier messages of the same conversation, from `all_mailboxes`)
    and `replied_before` (how often the reader has written to that sender,
    from \\Sent).

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
        # A Gmail thread id, or the References chain that stands in for it.
        want_threads = [em for em in items if em.get("thrid") or em.get("refs")][:thread_budget]
        want_senders = _sender_addresses(items, own, sender_budget)
        if not want_threads and not want_senders:
            continue
        pw = app_password(environ, account)
        if not pw:
            warnings.append(
                {"account": account, "error": f"no app password found in ${pw_env_var(account)}, skipping context"}
            )
            continue
        try:
            M, caps = connect(account, pw, host or imap_host(environ, account))
            try:
                if want_threads:
                    # One mailbox is enough for context: Gmail's \All, or INBOX.
                    M.select(_quote_mailbox(all_mailboxes(M, caps)[0]), readonly=True)
                    for em in want_threads:
                        lines, n = _thread_lines(M, caps, em, now)
                        fetches += n
                        thread_budget -= 1
                        if lines:
                            em["thread"] = lines
                            threads += 1
                if want_senders:
                    M.select(_quote_mailbox(sent_mailbox(M, caps)), readonly=True)
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


def _replied_in_sent(M: imaplib.IMAP4_SSL, caps: Caps, thrid: str, message_id: str) -> bool:
    """True when the user's Sent mailbox (must already be the selected
    mailbox) holds a message in the same Gmail thread -- or, on a server with
    no thread ids, one that's In-Reply-To the original message."""
    if caps.gmail and thrid:
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
    host: str = "",
    only: set[str] | None = None,
) -> PullResult:
    """Re-surface needs_action mail `label_actions` labeled on a prior run
    and that's still open: still carrying the label, older than the current
    window (an in-window hit is already covered by the normal `pull` path,
    so re-including it here would duplicate it), and with no reply from the
    user anywhere in its thread. Read-only throughout; makes no model call.
    """
    messages: list[Email] = []
    warnings: list[dict[str, str]] = []
    for addr, pw in accounts_from_env(environ, only):
        try:
            acct_host = host or imap_host(environ, addr)
            M, caps = connect(addr, pw, acct_host)
            try:
                select_inbox(M, caps)
                # In folders mode this re-selects the label's own mailbox --
                # the UIDs below are then that mailbox's, which is exactly
                # what the FETCH wants.
                uids = search_label(M, caps, label)

                candidates: list[tuple[Email, str]] = []  # (email, thrid)
                for uid in uids:
                    spec = "(FLAGS BODY.PEEK[] X-GM-THRID)" if caps.gmail else "(FLAGS BODY.PEEK[])"
                    # A raw UID as imaplib gets it everywhere else here: bytes,
                    # which the stub types as str-only but the library joins fine.
                    _, fetched = M.uid("FETCH", uid, spec)  # type: ignore[arg-type]
                    flags, raw = "", b""
                    for part in fetched:
                        if isinstance(part, tuple):
                            flags = part[0].decode("ascii", "replace")
                            raw = part[1]
                    rec = _parse_labeled_message(raw, addr, flags, uid.decode(), acct_host)
                    if rec is None:
                        continue
                    if not _older_than_window(datetime.fromisoformat(rec["date"]), now, window_hours):
                        continue  # still in-window -- the normal pull() path already covers it
                    candidates.append((rec, _extract_thrid(flags)))

                if candidates:
                    M.select(_quote_mailbox(sent_mailbox(M, caps)), readonly=True)
                    for rec, thrid in candidates:
                        if not _replied_in_sent(M, caps, thrid, rec["message_id"]):
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


def _parse_week_message(
    raw: bytes, addr: str, now: datetime, uid: str, host: str = GMAIL_IMAP
) -> dict[str, Any] | None:
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
        "link": webmail_link(host, addr, message_id),
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
    host: str = "",
    only: set[str] | None = None,
) -> WeekResult:
    """Weekly roll-up: everything carrying `label` in the last `days` days,
    per account, classified replied / archived / open by pure IMAP
    arithmetic -- no model call, see _classify_week_item.

    Searches `all_mailboxes` -- Gmail's \\All, or INBOX plus \\Archive, both
    found by special-use attribute -- so archived and replied items, which
    have left INBOX, are still found; a message sitting in two of them is
    de-duplicated by Message-ID. SINCE is date-granular; results are
    re-filtered exactly against `days` in Python, same as fetch_account.
    Only header fields are ever fetched (BODY.PEEK[HEADER.FIELDS ...]), no
    body. Read-only throughout: every select is `readonly=True`.

    An item whose label was removed since it was actioned is invisible to
    this search -- that's fine, its disappearance from next week's roll-up
    IS the "handled" signal; there is nothing to report for it here.
    """
    since = (now - timedelta(days=days) - timedelta(days=1)).strftime("%d-%b-%Y")
    cutoff = now - timedelta(days=days)
    accounts: dict[str, dict[str, list[WeekItem]]] = {}
    warnings: list[dict[str, str]] = []

    for addr, pw in accounts_from_env(environ, only):
        try:
            acct_host = host or imap_host(environ, addr)
            M, caps = connect(addr, pw, acct_host)
            try:
                candidates: list[tuple[dict[str, Any], str]] = []  # (rec, thrid)
                seen_ids: set[str] = set()
                for box in all_mailboxes(M, caps):
                    if M.select(_quote_mailbox(box), readonly=True)[0] != "OK":
                        continue
                    for uid in search_label(M, caps, label, "SINCE", since):
                        spec = (
                            "(FLAGS UID X-GM-THRID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
                            if caps.gmail
                            else "(FLAGS UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
                        )
                        _, fetched = M.uid("FETCH", uid, spec)  # type: ignore[arg-type]
                        flags, raw = "", b""
                        for part in fetched:
                            if isinstance(part, tuple):
                                flags = part[0].decode("ascii", "replace")
                                raw = part[1]
                        rec = _parse_week_message(raw, addr, now, uid.decode(), acct_host)
                        if rec is None or datetime.fromisoformat(rec["date"]) < cutoff:
                            continue
                        if rec["message_id"] and rec["message_id"] in seen_ids:
                            continue  # the same message, filed in two mailboxes
                        seen_ids.add(rec["message_id"])
                        candidates.append((rec, _extract_thrid(flags)))

                replied: list[WeekItem] = []
                archived: list[WeekItem] = []
                open_items: list[WeekItem] = []

                if candidates:
                    M.select(_quote_mailbox(sent_mailbox(M, caps)), readonly=True)
                    still_unreplied: list[tuple[dict[str, Any], str]] = []
                    for rec, thrid in candidates:
                        if _replied_in_sent(M, caps, thrid, rec["message_id"]):
                            replied.append(_to_week_item(rec))
                        else:
                            still_unreplied.append((rec, thrid))

                    if still_unreplied:
                        M.select("INBOX", readonly=True)
                        for rec, thrid in still_unreplied:
                            in_inbox = same_thread_present(M, caps, thrid, rec["message_id"])
                            bucket = _classify_week_item(False, in_inbox)
                            (open_items if bucket == "open" else archived).append(_to_week_item(rec))

                accounts[addr] = {"replied": replied, "archived": archived, "open": open_items}
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types — one bad account must not abort the rest
            warnings.append({"account": addr, "error": f"{type(e).__name__}: {e}"})

    return {"accounts": accounts, "warnings": warnings}
