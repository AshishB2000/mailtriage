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
        "date": str,  # ISO 8601
        "unread": bool,
        "link": str,
    },
)


class Triaged(TypedDict):
    """One surfaced email. `bucket`/`note` come from the model; the rest are
    copied verbatim from the source Email (never model-authored)."""

    bucket: str  # "needs_action" | "worth_reading"
    note: str  # the single model-authored line
    account: str
    sender: str
    subject: str
    link: str
    date: str
    unread: bool
