"""The mailbox as the control plane. The reader applies a label in Gmail
(phone or web) -- or the matching IMAP keyword in any other client -- or
replies to the digest in plain words; the next run reads it and acts. No
state anywhere but the reader's own mailbox.

Label names are fixed literals (not derived from cfg.label) so the digest
footer, the README and this module can't drift apart:

  mailtriage/done            stop carrying this item (cfg.label is removed)
  mailtriage/snooze-<N>d     hide it for N days (1..90); also snooze-1w, snooze-2w
  mailtriage/until-<date>    what a snooze becomes; wakes when the date arrives
  mailtriage/never           this message's sender is always_ignore from now on
  mailtriage/vip             this message's sender is always_surface from now on
  mailtriage/handled         a digest reply that has already been acted on

On a non-Gmail server each name becomes an IMAP keyword
(`imap_pull.keyword_for`: `mailtriage/done` -> `$MailtriageDone`), which is
what the reader's client shows as a tag. Every label read and write here goes
through the capability helpers in imap_pull -- this module never writes
`X-GM-*` itself -- and on a server with neither labels nor keywords the whole
control plane is skipped with one warning (see UNSUPPORTED below).

Writes: the read-write INBOX select is the same one label_actions uses; reply
commands additionally select the account's \\All / INBOX+\\Archive mailboxes
read-write so an item the reader already archived can still be labeled
never/vip. Nothing here sends mail, deletes a message, or marks one read --
the only DELETE is of an emptied until-<date> label mailbox, so the label
list doesn't grow forever.
"""

from __future__ import annotations

import contextlib
import html
import imaplib
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any
from urllib.parse import unquote

from mailtriage.config import Config
from mailtriage.errors import MailError
from mailtriage.imap_pull import (
    Caps,
    _parse_labeled_message,
    _quote_mailbox,
    accounts_from_env,
    all_mailboxes,
    connect,
    create_label,
    imap_host,
    label_criteria,
    push_drafts,
    search_label,
    select_inbox,
    store_label,
)
from mailtriage.models import Email, Triaged
from mailtriage.triage import CallFn

LABEL_DONE = "mailtriage/done"
LABEL_NEVER = "mailtriage/never"
LABEL_VIP = "mailtriage/vip"
LABEL_HANDLED = "mailtriage/handled"
SNOOZE_PREFIX = "mailtriage/snooze-"
UNTIL_PREFIX = "mailtriage/until-"
# Written next to every dated until-label. On Gmail the dated labels are
# mailboxes and can simply be listed; IMAP keywords cannot, so this constant
# marker is what a keyword server searches for to find sleeping mail again.
SLEEPING = "mailtriage/sleeping"
MAX_SNOOZE_DAYS = 90
# Created (idempotently) on every run so they're one tap away in Gmail's label
# picker; a reader can still type any snooze-<N>d by hand.
DEFAULT_LABELS = (
    LABEL_DONE,
    LABEL_NEVER,
    LABEL_VIP,
    f"{SNOOZE_PREFIX}1d",
    f"{SNOOZE_PREFIX}3d",
    f"{SNOOZE_PREFIX}1w",
    f"{SNOOZE_PREFIX}2w",
)
SENDER_CAP = 500  # never/vip derivation: newest N labeled messages per account, per label
COMMAND_ACTIONS = ("done", "snooze", "draft", "never", "vip")
DEFAULT_SNOOZE_DAYS = 7

COMMAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "commands": {
            "type": "array",
            "description": "Commands the note clearly asks for. May be empty.",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(COMMAND_ACTIONS)},
                    "item": {"type": "integer", "description": "The digest item number, without the #."},
                    "days": {
                        "type": "integer",
                        "description": "snooze only: how many days to hide it (1-90); 0 for any other action.",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "draft only: how the reader wants the reply changed; empty otherwise.",
                    },
                },
                "required": ["action", "item", "days", "instruction"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["commands"],
    "additionalProperties": False,
}

COMMAND_SYSTEM = """You turn a short note the reader wrote in reply to their email digest into commands. Digest items are numbered #1, #2, ...

ACTIONS
- done: they handled it -- stop reminding them.
- snooze: hide it for a number of days. "a week" is 7, "two weeks" is 14, "tomorrow" is 1; when no length is given, use 7.
- draft: rewrite the AI-drafted reply to that item following their instruction (put the instruction in `instruction`, in their words).
- never: stop showing mail from that item's sender.
- vip: always show mail from that item's sender.

RULES
- Only emit commands the note clearly asks for, and only for item numbers in the list given. Never guess an item number.
- If the note is not a command at all, return an empty list."""


# --- label name parsing (pure) -------------------------------------------


def snooze_days(label: str) -> int | None:
    """'mailtriage/snooze-3d' -> 3, '...-1w' -> 7, '...-2w' -> 14. None for
    anything else, including a length outside 1..MAX_SNOOZE_DAYS."""
    m = re.fullmatch(re.escape(SNOOZE_PREFIX) + r"(\d{1,2})([dw])", label)
    if not m:
        return None
    n = int(m.group(1)) * (7 if m.group(2) == "w" else 1)
    return n if 1 <= n <= MAX_SNOOZE_DAYS else None


def until_date(label: str) -> date | None:
    if not label.startswith(UNTIL_PREFIX):
        return None
    try:
        return date.fromisoformat(label[len(UNTIL_PREFIX) :])
    except ValueError:
        return None


def until_label(d: date) -> str:
    return f"{UNTIL_PREFIX}{d.isoformat()}"


# --- IMAP helpers ---------------------------------------------------------


def _mailbox_names(list_lines: list[Any]) -> list[str]:
    """Every mailbox (= Gmail label) name in a LIST response."""
    names: list[str] = []
    for entry in list_lines:
        line = entry[0] if isinstance(entry, tuple) else entry
        if not isinstance(line, bytes):
            continue
        m = re.search(rb'"([^"]*)"\s*$', line)
        if m:
            names.append(m.group(1).decode("utf-8", "replace"))
        elif line.split():
            names.append(line.split()[-1].decode("utf-8", "replace"))
    return names


# A server with neither Gmail labels nor IMAP keywords can't carry a
# reader's tag at all: the control plane is skipped with this one warning
# per account, and triage itself still runs.
UNSUPPORTED = (
    "this server has neither Gmail labels nor IMAP keywords (PERMANENTFLAGS has no \\*), "
    "so done/snooze/never/vip labels and digest replies are skipped here"
)


def _search(M: imaplib.IMAP4_SSL, *criteria: str) -> list[bytes]:
    # None = default charset, same imaplib stub quirk as imap_pull._replied_in_sent.
    _, data = M.uid("SEARCH", None, *criteria)  # type: ignore[arg-type]
    return data[0].split() if data and data[0] else []


def _store(M: imaplib.IMAP4_SSL, caps: Caps, uid: bytes | str, op: str, label: str) -> None:
    """`op` is "+" or "-" -- imap_pull.store_label decides what a label IS on
    this server (Gmail label or IMAP keyword; this module never runs in the
    folders fallback, see UNSUPPORTED)."""
    store_label(M, caps, uid, label, add=op == "+")


def _create(M: imaplib.IMAP4_SSL, caps: Caps, label: str) -> None:
    create_label(M, caps, label)


def label_from_keyword(keyword: str) -> str:
    """The inverse of `imap_pull.keyword_for` for the two label shapes that
    carry data: `$MailtriageSnooze3d` -> `mailtriage/snooze-3d`,
    `$MailtriageUntil20260910` -> `mailtriage/until-2026-09-10`. "" for
    anything else -- a keyword the reader invented is not a command."""
    m = re.fullmatch(r"\$MailtriageSnooze(\d{1,2})([dw])", keyword, re.IGNORECASE)
    if m:
        return f"{SNOOZE_PREFIX}{m.group(1)}{m.group(2).lower()}"
    m = re.fullmatch(r"\$MailtriageUntil(\d{4})(\d{2})(\d{2})", keyword, re.IGNORECASE)
    if m:
        return f"{UNTIL_PREFIX}{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def _present_labels(M: imaplib.IMAP4_SSL, caps: Caps, action_label: str) -> list[str]:
    """The snooze/until labels actually in use in the selected INBOX.

    Gmail lists them as mailboxes. IMAP keywords can't be listed at all, so
    they are read off the messages that carry one of ours instead: one search
    for the action tag plus one for the SLEEPING marker, then a single
    FLAGS-only FETCH (never a body).
    """
    if caps.gmail:
        return [n for n in _mailbox_names(M.list()[1] or []) if n.startswith("mailtriage/")]
    uids = search_label(M, caps, action_label) + search_label(M, caps, SLEEPING)
    if not uids:
        return []
    _, fetched = M.uid("FETCH", b",".join(sorted(set(uids))).decode(), "(FLAGS)")
    found: list[str] = []
    for part in fetched:
        line = part[0] if isinstance(part, tuple) else part
        if not isinstance(line, bytes):
            continue
        for kw in re.findall(r"\$\w+", line.decode("ascii", "replace")):
            label = label_from_keyword(kw)
            if label and label not in found:
                found.append(label)
    return found


def _fetch_raw(M: imaplib.IMAP4_SSL, uid: bytes, what: str) -> tuple[str, bytes]:
    _, fetched = M.uid("FETCH", uid.decode(), what)
    flags, raw = "", b""
    for part in fetched:
        if isinstance(part, tuple):
            flags = part[0].decode("ascii", "replace")
            raw = part[1]
    return flags, raw


def _accounts(environ: Mapping[str, str], warnings: list[dict[str, str]]) -> list[tuple[str, str]]:
    # Tolerant on purpose: pull() has already raised the useful error when
    # MAIL_ACCOUNTS is unset; here it must only warn, never abort the run.
    try:
        return accounts_from_env(environ)
    except MailError as e:
        warnings.append({"account": "", "error": str(e)})
        return []


# --- 1. label commands ----------------------------------------------------


def apply_label_commands(environ: Mapping[str, str], today: date, action_label: str, host: str = "") -> dict[str, Any]:
    """Act on done/snooze/until labels in each INBOX. Idempotent -- every
    write is "make it so", and a label that doesn't exist is simply nothing
    to do.

    Returns {"counts": {done, snoozed, woken}, "skip": {account: {inbox uid}},
    "warnings": [...]}. `skip` is every message the reader has marked done or
    that is still snoozed: cli.run drops those from this run's candidates, or
    an in-window item the reader just closed would be re-triaged and
    re-labeled as if nothing happened.

    An account whose server has neither Gmail labels nor IMAP keywords is
    skipped with one UNSUPPORTED warning -- there is nothing for the reader
    to have tagged.
    """
    counts = {"done": 0, "snoozed": 0, "woken": 0}
    skip: dict[str, set[str]] = {}
    warnings: list[dict[str, str]] = []
    for addr, pw in _accounts(environ, warnings):
        try:
            M, caps = connect(addr, pw, host or imap_host(environ, addr))
            try:
                # read-write on purpose: STORE needs it, same as label_actions.
                # This is also where PERMANENTFLAGS tells us about keywords.
                select_inbox(M, caps, readonly=False)
                if caps.mode == "folders":
                    warnings.append({"account": addr, "error": UNSUPPORTED})
                    continue
                for label in DEFAULT_LABELS:
                    _create(M, caps, label)
                labels = _present_labels(M, caps, action_label)
                skipped = skip.setdefault(addr, set())

                for uid in search_label(M, caps, LABEL_DONE, *label_criteria(caps, action_label)):
                    _store(M, caps, uid, "-", action_label)
                    counts["done"] += 1
                skipped.update(u.decode() for u in search_label(M, caps, LABEL_DONE))

                for label in labels:
                    days = snooze_days(label)
                    if days is None:
                        continue
                    uids = search_label(M, caps, label)
                    if not uids:
                        continue
                    target = until_label(today + timedelta(days=days))
                    _create(M, caps, target)
                    for uid in uids:
                        _store(M, caps, uid, "+", target)
                        if not caps.gmail:
                            _store(M, caps, uid, "+", SLEEPING)  # the only way to find it again
                        _store(M, caps, uid, "-", label)
                        _store(M, caps, uid, "-", action_label)
                        skipped.add(uid.decode())
                        counts["snoozed"] += 1

                for label in labels:
                    due = until_date(label)
                    if due is None:
                        continue
                    uids = search_label(M, caps, label)
                    if due <= today:
                        for uid in uids:
                            _store(M, caps, uid, "+", action_label)
                            _store(M, caps, uid, "-", label)
                            if not caps.gmail:
                                _store(M, caps, uid, "-", SLEEPING)
                            counts["woken"] += 1
                        if caps.gmail:
                            with contextlib.suppress(Exception):  # now empty -- keep the label list short
                                M.delete(_quote_mailbox(label))
                    else:
                        for uid in uids:  # still asleep: guarantee carry-over can't see it
                            _store(M, caps, uid, "-", action_label)
                            skipped.add(uid.decode())
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types -- one bad account must not abort the rest
            warnings.append({"account": addr, "error": f"{type(e).__name__}: {e}"})
    return {"counts": counts, "skip": skip, "warnings": warnings}


def derive_sender_rules(environ: Mapping[str, str], host: str = "") -> dict[str, Any]:
    """Senders of messages labeled never/vip anywhere in `all_mailboxes`
    (Gmail's \\All, or INBOX + \\Archive), as lowercased full addresses.
    Read-only, header-only (FROM), capped at SENDER_CAP newest per label per
    mailbox. Returns {"never": set, "vip": set, "warnings"}."""
    out: dict[str, Any] = {"never": set(), "vip": set(), "warnings": []}
    for addr, pw in _accounts(environ, out["warnings"]):
        try:
            M, caps = connect(addr, pw, host or imap_host(environ, addr))
            try:
                for box in all_mailboxes(M, caps):
                    if M.select(_quote_mailbox(box), readonly=True)[0] != "OK":
                        continue
                    for key, label in (("never", LABEL_NEVER), ("vip", LABEL_VIP)):
                        uids = search_label(M, caps, label)[-SENDER_CAP:]
                        if not uids:
                            continue
                        _, fetched = M.uid("FETCH", b",".join(uids).decode(), "(BODY.PEEK[HEADER.FIELDS (FROM)])")
                        for part in fetched:
                            if not isinstance(part, tuple):
                                continue
                            msg = message_from_bytes(part[1], policy=policy.default)
                            _, sender = parseaddr(str(msg.get("From", "")))
                            if sender:
                                out[key].add(sender.lower())
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types -- one bad account must not abort the rest
            out["warnings"].append({"account": addr, "error": f"{type(e).__name__}: {e}"})
    return out


def with_sender_rules(cfg: Config, never: set[str], vip: set[str]) -> Config:
    """cfg with the label-derived senders merged into rules.always_ignore /
    always_surface -- rules.matches does the rest (case-insensitive, full
    address). always_action still beats a `never` label, as it beats
    always_ignore."""
    if not never and not vip:
        return cfg
    rules = {
        **cfg.rules,
        "always_ignore": cfg.rules.get("always_ignore", []) + sorted(never),
        "always_surface": cfg.rules.get("always_surface", []) + sorted(vip),
    }
    return replace(cfg, rules=rules)


def count_done(environ: Mapping[str, str], now: datetime, days: int = 7, host: str = "") -> dict[str, Any]:
    """How many messages got the done label in the last `days` days, across
    `all_mailboxes` (a done item has lost cfg.label, so pull_week can't see
    it). Search only, nothing fetched. Returns {"done": n, "warnings": [...]}."""
    since = (now - timedelta(days=days)).strftime("%d-%b-%Y")
    out: dict[str, Any] = {"done": 0, "warnings": []}
    for addr, pw in _accounts(environ, out["warnings"]):
        try:
            M, caps = connect(addr, pw, host or imap_host(environ, addr))
            try:
                for box in all_mailboxes(M, caps):
                    if M.select(_quote_mailbox(box), readonly=True)[0] != "OK":
                        continue
                    out["done"] += len(search_label(M, caps, LABEL_DONE, "SINCE", since))
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types -- one bad account must not abort the rest
            out["warnings"].append({"account": addr, "error": f"{type(e).__name__}: {e}"})
    return out


# --- 2. reply to the digest (pure parsing) --------------------------------

# The digest renders each item's number as a link to the message itself:
# <a href="https://mail.google.com/mail/u/ACCOUNT/#search/rfc822msgid:MID">#N</a>.
# Gmail keeps that HTML intact inside the reply's quoted blockquote, and its
# plain-text rendition becomes "#N <URL>" -- one regex per shape, each
# requiring the number and the link to be adjacent, so a "#3" the reader
# typed in their own text can never pair with the wrong link.
_LINK = r"https://mail\.google\.com/mail/u/([^/\s\"'<>]+)/#search/rfc822msgid:([^\s\"'<>)]+)"
_HTML_ITEM_RE = re.compile(_LINK + r'"[^>]*>\s*#(\d{1,3})\s*</a>')
_TEXT_ITEM_RE = re.compile(r"#(\d{1,3})\s*<?\s*" + _LINK)
# Other mailboxes: imap_pull.webmail_link's non-Gmail shapes. They carry the
# Message-ID but no account -- handle_replies acts in the mailbox the reply
# arrived at.
_ALT_LINK = r"(?:https://app\.fastmail\.com/mail/search:msgid%3A|message:)([^\s\"'<>)]+)"
_HTML_ALT_RE = re.compile(_ALT_LINK + r'"[^>]*>\s*#(\d{1,3})\s*</a>')
_TEXT_ALT_RE = re.compile(r"#(\d{1,3})\s*<?\s*" + _ALT_LINK)
_QUOTE_START_RE = re.compile(r"^(>|On\b.*|From:\s|-{3,}\s*(Original|Forwarded))", re.IGNORECASE)


def item_map(html_part: str, text_part: str) -> dict[int, tuple[str, str]]:
    """#N -> (account, Message-ID) from the quoted digest. HTML wins when it
    yields anything; the plain-text rendition is the fallback."""
    out: dict[int, tuple[str, str]] = {}
    for m in _HTML_ITEM_RE.finditer(html_part):
        account, mid, n = m.group(1), m.group(2), int(m.group(3))
        out.setdefault(n, (unquote(html.unescape(account)), unquote(html.unescape(mid))))
    if out:
        return out
    for m in _TEXT_ITEM_RE.finditer(text_part):
        n, account, mid = int(m.group(1)), m.group(2), m.group(3)
        out.setdefault(n, (unquote(account), unquote(mid)))
    if out:
        return out
    for m in _HTML_ALT_RE.finditer(html_part):
        out.setdefault(int(m.group(2)), ("", unquote(html.unescape(m.group(1)))))
    for m in _TEXT_ALT_RE.finditer(text_part):
        out.setdefault(int(m.group(1)), ("", unquote(m.group(2))))
    return out


def user_text(text_part: str) -> str:
    """The reader's own words: everything above the first quoted line. Gmail's
    "On <date> <who> wrote:" attribution can wrap onto a second line, so an
    "On ..." line counts when "wrote:" appears within the next three lines."""
    lines = text_part.splitlines()
    kept: list[str] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if _QUOTE_START_RE.match(s) and (
            not s.startswith("On") or "wrote:" in " ".join(x.strip() for x in lines[i : i + 3])
        ):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _parts(msg: EmailMessage) -> tuple[str, str]:
    """(text/plain, text/html) bodies, each "" when absent."""
    plain, html_ = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html") or "attachment" in str(part.get("Content-Disposition", "")):
            continue
        try:
            body = str(part.get_content())
        except Exception:
            payload = part.get_payload(decode=True)
            body = (
                payload.decode(part.get_content_charset() or "utf-8", "replace") if isinstance(payload, bytes) else ""
            )
        if ctype == "text/plain" and not plain:
            plain = body
        elif ctype == "text/html" and not html_:
            html_ = body
    if not plain and html_:
        plain = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html_)))
    return plain, html_


def build_command_user(items: dict[int, tuple[str, str]], text: str) -> str:
    numbers = ", ".join(f"#{n}" for n in sorted(items)) or "(none)"
    return f"Items in the digest: {numbers}\n\nThe reader wrote:\n{text[:2000]}"


def parse_commands(reply: dict[str, Any], items: dict[int, tuple[str, str]]) -> list[dict[str, Any]]:
    """Validate the model's commands like pick() validates its items: unknown
    action, non-int/bool/unknown item, and duplicates are dropped; days falls
    back to DEFAULT_SNOOZE_DAYS outside 1..MAX_SNOOZE_DAYS; instruction is
    coerced to a capped string."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    got_list = reply.get("commands")
    for got in got_list if isinstance(got_list, list) else []:
        if not isinstance(got, dict):
            continue
        action, item = got.get("action"), got.get("item")
        if action not in COMMAND_ACTIONS or not isinstance(item, int) or isinstance(item, bool) or item not in items:
            continue
        if (action, item) in seen:
            continue
        seen.add((action, item))
        days = got.get("days")
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_SNOOZE_DAYS:
            days = DEFAULT_SNOOZE_DAYS
        instruction = got.get("instruction")
        out.append(
            {
                "action": action,
                "item": item,
                "days": days,
                "instruction": str(instruction)[:500] if instruction else "",
            }
        )
    return out


# --- 2. reply to the digest (IMAP) ----------------------------------------


def _own_addresses(cfg: Config, accounts: list[tuple[str, str]]) -> set[str]:
    own = {a.lower() for a, _ in accounts} | {cfg.email_to.strip().lower(), cfg.email_from.strip().lower()}
    return own - {""}


def _redraft(
    cfg: Config, call: CallFn, environ: Mapping[str, str], M: imaplib.IMAP4_SSL, uid: bytes, addr: str, instruction: str
) -> list[dict[str, str]]:
    """Regenerate one item's draft with the reader's instruction folded into
    its draft style, and push it. The earlier draft stays in Gmail Drafts --
    the reader deletes whichever they don't want."""
    flags, raw = _fetch_raw(M, uid, "(FLAGS BODY.PEEK[])")
    em: Email | None = _parse_labeled_message(raw, addr, flags, uid.decode())
    if em is None:
        return [{"account": addr, "error": "draft target has no Date header, skipping"}]
    account = cfg.accounts.get(addr.lower(), {})
    style = {**account.get("draft_style", cfg.draft_style), "instruction": instruction}
    accounts = {a: {k: v for k, v in acc.items() if k != "draft_style"} for a, acc in cfg.accounts.items()}
    cfg_i = replace(cfg, draft_style=style, accounts=accounts)
    triaged: Triaged = {
        "bucket": "needs_action",
        "note": "reply",
        "account": em["account"],
        "sender": em["from"],
        "subject": em["subject"],
        "link": em["link"],
        "date": em["date"],
        "unread": em["unread"],
        "idx": 0,
        "draft": "",
    }
    from mailtriage.drafts import generate_drafts  # lazy, like cli.run: keeps --self-check import-light

    generate_drafts(cfg_i, call, [em], [triaged])
    if not triaged["draft"]:
        return [{"account": addr, "error": "model returned no draft for the redraft request"}]
    return push_drafts(environ, [triaged], [em])


def _apply_commands(
    cfg: Config,
    call: CallFn,
    environ: Mapping[str, str],
    today: date,
    accounts: dict[str, str],
    commands: list[tuple[str, str, dict[str, Any]]],  # (account, message_id, command)
    counts: dict[str, int],
    host: str,
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    by_account: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for account, mid, cmd in commands:
        by_account.setdefault(account.lower(), []).append((mid, cmd))
    for account, todo in by_account.items():
        pw = accounts.get(account)
        if not pw:
            counts["skipped"] += len(todo)  # a digest item from an account this fork can't write to
            continue
        try:
            M, caps = connect(account, pw, host or imap_host(environ, account))
            try:
                # read-write: STORE needs it. The reader may have archived the
                # item already, so look through every "everything" mailbox --
                # UIDs are per-mailbox, so act in whichever one has it.
                boxes = all_mailboxes(M, caps)
                for mid, cmd in todo:
                    uids: list[bytes] = []
                    for box in boxes:
                        if M.select(_quote_mailbox(box))[0] != "OK":
                            continue
                        uids = _search(M, "HEADER", "Message-ID", _quote_mailbox(mid))
                        if uids:
                            break
                    if not uids:
                        counts["skipped"] += 1
                        continue
                    uid, action = uids[0], cmd["action"]
                    if action == "done":
                        _create(M, caps, LABEL_DONE)
                        _store(M, caps, uid, "+", LABEL_DONE)
                        _store(M, caps, uid, "-", cfg.label)
                    elif action == "snooze":
                        target = until_label(today + timedelta(days=cmd["days"]))
                        _create(M, caps, target)
                        _store(M, caps, uid, "+", target)
                        if not caps.gmail:
                            _store(M, caps, uid, "+", SLEEPING)
                        _store(M, caps, uid, "-", cfg.label)
                    elif action == "never":
                        _create(M, caps, LABEL_NEVER)
                        _store(M, caps, uid, "+", LABEL_NEVER)
                    elif action == "vip":
                        _create(M, caps, LABEL_VIP)
                        _store(M, caps, uid, "+", LABEL_VIP)
                    elif action == "draft":
                        warnings.extend(_redraft(cfg, call, environ, M, uid, account, cmd["instruction"]))
                    counts[action] += 1
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types -- one bad account must not abort the rest
            warnings.append({"account": account, "error": f"{type(e).__name__}: {e}"})
    return warnings


def handle_replies(
    cfg: Config,
    environ: Mapping[str, str],
    now: datetime,
    today: date,
    backend: Callable[[], CallFn],
    host: str = "",
) -> dict[str, Any]:
    """Find un-handled replies to the digest in each INBOX (from one of the
    reader's own addresses, subject "Re: ..." containing cfg.subject_prefix),
    turn each into commands via the model, apply them, then label the reply
    mailtriage/handled so it is acted on exactly once.

    `backend` is resolved lazily -- only a run that actually has a reply pays
    for provider selection. Returns {"replies": n, "counts": {action: n,
    "skipped": n}, "skip_message_ids": {...}, "warnings": [...]}; cli.run
    drops the replies themselves from this run's triage candidates.
    """
    counts = {a: 0 for a in COMMAND_ACTIONS} | {"skipped": 0}
    out: dict[str, Any] = {"replies": 0, "counts": counts, "skip_message_ids": set(), "warnings": []}
    account_list = _accounts(environ, out["warnings"])
    accounts = {a.lower(): pw for a, pw in account_list}
    own = _own_addresses(cfg, account_list)
    since = (now - timedelta(days=7)).strftime("%d-%b-%Y")
    call: CallFn | None = None

    for addr, pw in account_list:
        try:
            M, caps = connect(addr, pw, host or imap_host(environ, addr))
            try:
                select_inbox(M, caps, readonly=False)  # read-write: the handled label is STOREd below
                if caps.mode == "folders":
                    out["warnings"].append({"account": addr, "error": UNSUPPORTED})
                    continue
                uids = _search(
                    M,
                    "SUBJECT",
                    _quote_mailbox(cfg.subject_prefix),
                    "NOT",
                    *label_criteria(caps, LABEL_HANDLED),
                    "SINCE",
                    since,
                )
                for uid in uids:
                    _, raw = _fetch_raw(M, uid, "(BODY.PEEK[])")
                    msg = message_from_bytes(raw, policy=policy.default)
                    _, sender = parseaddr(str(msg.get("From", "")))
                    if not str(msg.get("Subject", "")).strip().lower().startswith("re:") or sender.lower() not in own:
                        continue
                    out["replies"] += 1
                    out["skip_message_ids"].add(str(msg.get("Message-ID", "")))
                    plain, html_ = _parts(msg)
                    items = item_map(html_, plain)
                    text = user_text(plain)
                    commands: list[tuple[str, str, dict[str, Any]]] = []
                    if items and text:
                        if call is None:
                            call = backend()
                        reply = call(cfg, COMMAND_SYSTEM, build_command_user(items, text), COMMAND_SCHEMA)
                        for cmd in parse_commands(reply, items):
                            account, mid = items[cmd["item"]]
                            # A non-Gmail digest link carries no account, so
                            # the item is acted on in the mailbox the reply
                            # arrived at -- right for one account, and a
                            # counted "skipped" when it belongs to another.
                            commands.append((account or addr, mid, cmd))
                    if commands:
                        assert call is not None
                        out["warnings"].extend(
                            _apply_commands(cfg, call, environ, today, accounts, commands, counts, host)
                        )
                    _create(M, caps, LABEL_HANDLED)
                    _store(M, caps, uid, "+", LABEL_HANDLED)
            finally:
                with contextlib.suppress(Exception):
                    M.logout()
        except Exception as e:  # imaplib raises many unrelated types -- one bad account must not abort the rest
            out["warnings"].append({"account": addr, "error": f"{type(e).__name__}: {e}"})
    return out
