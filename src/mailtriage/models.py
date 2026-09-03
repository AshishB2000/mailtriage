"""Typed shapes: Email (triage input) and Triaged (triage output)."""

from typing import TypedDict

# The record imap_pull emits per message. `from` is a keyword → functional form.
_EmailBase = TypedDict(
    "_EmailBase",
    {
        "account": str,
        "from": str,
        "subject": str,
        "snippet": str,
        "body": str,  # fuller text than snippet, for drafting replies
        "date": str,  # ISO 8601
        "unread": bool,
        "link": str,
        "message_id": str,  # raw Message-ID header, for In-Reply-To/References when drafting
        "reply_to": str,  # Reply-To header, falling back to From
        "uid": str,  # IMAP UID, for label/draft stages to address the message without re-searching; "" if synthetic
    },
)


class Email(_EmailBase, total=False):
    """Context added on top of the fetch. Optional keys (total=False) so a
    synthetic Email in a test, or one from a stage that never enriches, stays
    valid -- readers use .get() with a falsy default."""

    thrid: str  # Gmail X-GM-THRID from the fetch; "" when the server didn't return one
    thread: list[str]  # imap_pull.enrich: "<age> · <from>: <snippet>" for up to 2 earlier thread messages, oldest first
    attachments: list[str]  # "invoice.pdf (application/pdf)" per attached or named part, from the fetch itself
    replied_before: int  # imap_pull.enrich: messages the reader sent to this sender in the last 180 days


class PullResult(TypedDict):
    """Return shape of `imap_pull.pull`: collected messages plus per-account warnings."""

    messages: list["Email"]
    warnings: list[dict[str, str]]


class EnrichResult(TypedDict):
    """Return shape of `imap_pull.enrich`: counts only (they get printed to a
    public Actions log) plus the same per-account warnings as PullResult."""

    threads: int  # candidates that got earlier-thread context
    fetches: int  # extra IMAP FETCH round trips spent on it
    senders: int  # distinct senders looked up in \Sent
    warnings: list[dict[str, str]]


class _TriagedOptional(TypedDict, total=False):
    # Split out so `due` stays optional on Python 3.10 (no typing.NotRequired):
    # carried and rule-forced items never have one, and every existing
    # Triaged literal stays valid. Read it with t.get("due", "").
    due: str  # "YYYY-MM-DD" or "": model-authored, validated by triage.pick()
    draft_full: str  # the longer variant when draft_variants == 2; `draft` is then the short one


class Triaged(_TriagedOptional):
    """One surfaced email. `bucket`/`note`/`draft`/`due` come from the model;
    the rest are copied verbatim from the source Email (never model-authored)."""

    bucket: str  # "needs_action" | "worth_reading" from the model; "carried" is
    # client-authored only, by imap_pull.pull_open_actions re-surfacing a prior
    # run's still-open needs_action mail -- the model's own bucket enum
    # (triage.BUCKETS) is unchanged, so triage.pick() keeps rejecting it.
    note: str  # the single model-authored line
    account: str
    sender: str
    subject: str
    link: str
    date: str
    unread: bool
    idx: int  # index of the source Email in the pulled list, set by pick() from the validated id
    draft: str  # AI-drafted reply for needs_action items; "" when none


class WeekItem(TypedDict):
    """One item in imap_pull.pull_week's roll-up. Header-only -- no body is
    ever fetched for the weekly review, so there is no `snippet`/`body` here."""

    account: str
    sender: str
    subject: str
    date: str  # ISO 8601
    link: str
    age_days: int


class WeekResult(TypedDict):
    """Return shape of `imap_pull.pull_week`: per-account replied/archived/open
    buckets (each a list[WeekItem]) plus per-account warnings, same
    warn-and-continue shape as PullResult."""

    accounts: dict[str, dict[str, list["WeekItem"]]]
    warnings: list[dict[str, str]]
