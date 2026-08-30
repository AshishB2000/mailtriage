"""Typed shapes: Email (triage input) and Triaged (triage output)."""

from typing import TypedDict

# The record imap_pull emits per message. `from` is a keyword → functional form.
Email = TypedDict(
    "Email",
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
    },
)


class PullResult(TypedDict):
    """Return shape of `imap_pull.pull`: collected messages plus per-account warnings."""

    messages: list["Email"]
    warnings: list[dict[str, str]]


class Triaged(TypedDict):
    """One surfaced email. `bucket`/`note`/`draft` come from the model; the
    rest are copied verbatim from the source Email (never model-authored)."""

    bucket: str  # "needs_action" | "worth_reading"
    note: str  # the single model-authored line
    account: str
    sender: str
    subject: str
    link: str
    date: str
    unread: bool
    idx: int  # index of the source Email in the pulled list, set by pick() from the validated id
    draft: str  # AI-drafted reply for needs_action items; "" when none
