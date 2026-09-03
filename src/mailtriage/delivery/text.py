"""Plain-text rendering of the digest, shared by every delivery.

The chat channels (telegram/slack/discord/ntfy) and the `digest_format: text`
email all show the same three sections in the same order as the HTML digest;
they differ only in how bold and links are spelled. `render` takes those two
spellings as callables so the structure lives in exactly one place.
"""

from __future__ import annotations

from collections.abc import Callable
from html.parser import HTMLParser

from mailtriage.models import Triaged

DRAFT_PREVIEW = 200  # chars of an AI draft shown inline; the full text is in Gmail Drafts

SECTIONS: tuple[tuple[str, str], ...] = (
    ("Needs action", "needs_action"),
    ("Still waiting on you", "carried"),
    ("Worth reading", "worth_reading"),
)


def _plain(s: str) -> str:
    return s


def render(
    kept: list[Triaged],
    *,
    title: str = "",
    bold: Callable[[str], str] = _plain,
    link: Callable[[str, str], str] | None = None,
    esc: Callable[[str], str] = _plain,
) -> str:
    """Sections of `subject — sender — note` lines, one paragraph per item.
    `esc` runs on every user-controlled string BEFORE `bold`/`link` wrap it,
    so markup can never be smuggled in through a subject. With no `link`
    spelling the URL goes on its own line under the item (plain text);
    with one, the subject itself becomes the link."""
    parts: list[str] = []
    if title:
        parts.append(bold(esc(title)))
    for heading, bucket in SECTIONS:
        items = [t for t in kept if t["bucket"] == bucket]
        if not items:
            continue
        block = [bold(esc(heading))]
        for it in items:
            subject = esc(it["subject"]) or "(no subject)"
            head = link(subject, it["link"]) if link else subject
            for extra in (it["sender"], it["note"]):
                if extra:
                    head += f" — {esc(extra)}"
            lines = [head]
            if not link:
                lines.append(it["link"])
            if it["draft"]:
                draft = " ".join(it["draft"].split())
                if len(draft) > DRAFT_PREVIEW:
                    draft = draft[: DRAFT_PREVIEW - 1] + "…"
                lines.append(f"Draft: {esc(draft)}")
            block.append("\n".join(lines))
        parts.append("\n\n".join(block))
    return "\n\n".join(parts)


def digest_text(kept: list[Triaged]) -> str:
    """The plain-text digest: no markup, URL under each item."""
    return render(kept)


def chunk(text: str, limit: int) -> list[str]:
    """Split on paragraph boundaries so every piece fits `limit`, hard-splitting
    any single paragraph that is itself longer than that."""
    chunks: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        candidate = f"{cur}\n\n{para}" if cur else para
        if len(candidate) <= limit:
            cur = candidate
            continue
        if cur:
            chunks.append(cur)
        cur = para
        while len(cur) > limit:
            chunks.append(cur[:limit])
            cur = cur[limit:]
    if cur:
        chunks.append(cur)
    return chunks


class _TextOf(HTMLParser):
    """Block tags become line breaks, links get their URL on the next line,
    <style>/<head> content is dropped. Enough for the weekly review's own
    HTML; not a general converter."""

    BLOCK = frozenset({"p", "div", "tr", "li", "h1", "h2", "h3", "br", "table", "ul"})
    SKIP = frozenset({"style", "script", "head", "title"})

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.href = ""
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a":
            self.href = dict(attrs).get("href") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip -= 1
        if tag == "a" and self.href:
            self.out.append(f"\n{self.href}")
            self.href = ""
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def html_to_text(html_body: str) -> str:
    """Plain text of a delivery HTML body -- how the chat channels and the
    text email carry a prebuilt HTML (the weekly review) without a second
    renderer per channel."""
    p = _TextOf()
    p.feed(html_body)
    p.close()
    lines = [" ".join(line.split()) for line in "".join(p.out).split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):  # collapse runs of blank lines to one
            out.append(line)
    return "\n".join(out).strip()
