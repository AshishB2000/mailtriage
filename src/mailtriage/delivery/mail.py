"""Email delivery via Resend.

Named ``mail`` rather than ``email`` so nothing in this package can shadow the
stdlib ``email`` module for a reader skimming imports.
"""

from __future__ import annotations

import html
import os
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from mailtriage.config import Config
from mailtriage.delivery.http import post_json
from mailtriage.delivery.text import digest_text, html_to_text
from mailtriage.errors import MailError
from mailtriage.models import Triaged, WeekItem, WeekResult
from mailtriage.schedule import local_zone

INK, DIM, RULE, PAPER = "#16161a", "#6b6b76", "#e4e2dd", "#faf9f7"
SERIF = "Georgia,'Times New Roman',serif"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"


def _draft_block(draft: str, has_full: bool = False) -> str:
    if not draft:
        return ""
    fuller = (
        f'<div style="font:400 12px/1.4 {SANS};color:{DIM};margin:6px 0 0 0;">A fuller version is in Drafts.</div>'
        if has_full
        else ""
    )
    # white-space:pre-wrap keeps the model's paragraph breaks without a <br>-injection risk.
    return f"""
        <div class="muted" style="margin:10px 0 0 0;padding:10px 12px;border-left:2px solid {RULE};">
          <div style="font:700 11px/1 {SANS};letter-spacing:.08em;color:{DIM};text-transform:uppercase;">Draft reply</div>
          <p style="font:400 14px/1.5 {SANS};color:{DIM};margin:6px 0 0 0;white-space:pre-wrap;">{html.escape(draft)}</p>{fuller}
        </div>"""


GROUPS = ("Overdue", "Today", "This week", "Later", "No date")


def due_date(it: Triaged) -> date | None:
    d = it.get("due", "")
    return date.fromisoformat(d) if d else None  # pick() already validated the shape


def calendar_link(it: Triaged) -> str:
    """Google Calendar "add event" URL for a dated item: an all-day event on
    the due date whose details link back to the message in Gmail."""
    d = due_date(it)
    if d is None:
        return ""
    q = urlencode(
        {
            "action": "TEMPLATE",
            "text": it["subject"],
            "dates": f"{d:%Y%m%d}/{d + timedelta(days=1):%Y%m%d}",
            "details": it["link"],
        }
    )
    return "https://calendar.google.com/calendar/render?" + q


def digest_groups(triaged: list[Triaged], today: date) -> list[tuple[str, str, list[Triaged]]]:
    """The digest's sections in render order, as (kind, heading, items) with
    kind in action/carried/reading. Items are numbered #1.. in exactly this
    order by every renderer (HTML and --dry-run text), and a reply to the
    digest addresses them by that number -- so the order lives here, once.

    needs_action splits into Overdue / Today / This week / Later / No date
    (each sorted by due) only when at least one item has a due date; a
    digest with no deadlines renders exactly as before."""
    needs_action = [t for t in triaged if t["bucket"] == "needs_action"]
    carried = [t for t in triaged if t["bucket"] == "carried"]
    worth_reading = [t for t in triaged if t["bucket"] == "worth_reading"]
    out: list[tuple[str, str, list[Triaged]]] = []
    if any(due_date(t) for t in needs_action):
        buckets: dict[str, list[Triaged]] = {g: [] for g in GROUPS}
        for t in needs_action:
            d = due_date(t)
            if d is None:
                g = "No date"
            elif d < today:
                g = "Overdue"
            elif d == today:
                g = "Today"
            elif d <= today + timedelta(days=6):
                g = "This week"
            else:
                g = "Later"
            buckets[g].append(t)
        for g, items in buckets.items():
            if items:
                items.sort(key=lambda t: t.get("due", ""))  # stable: undated keep the model's order
                out.append(("action", f"Needs action · {g}", items))
    elif needs_action:
        out.append(("action", "Needs action", needs_action))
    if carried:
        out.append(("carried", "Still waiting on you", carried))
    if worth_reading:
        out.append(("reading", "Worth reading", worth_reading))
    return out


def waiting_days(date_iso: str, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return max(0, (now - datetime.fromisoformat(date_iso)).days)


def _number(n: int, link: str) -> str:
    # The item number IS a link to the message: a reply to the digest quotes
    # this anchor, and commands.item_map reads "#N" straight off its href.
    return f'<a href="{html.escape(link, quote=True)}" class="muted" style="font:400 13px/1.35 {SANS};color:{DIM};text-decoration:none;">#{n}</a> '


def _due_line(it: Triaged) -> str:
    d = due_date(it)
    if d is None:
        return ""
    return f"""
        <div class="muted" style="font:400 13px/1.4 {SANS};color:{DIM};padding-top:4px;">Due {d:%a} {d.day} {d:%b} &nbsp;·&nbsp; <a href="{html.escape(calendar_link(it), quote=True)}" style="color:{DIM};">Add to Google Calendar</a></div>"""


def _rows(items: list[Triaged], start: int) -> str:
    out = ""
    for n, it in enumerate(items, start):
        dot = "&#9679; " if it["unread"] else ""
        out += f"""
      <tr><td style="padding:0 0 26px 0;">
        {_number(n, it["link"])}<a href="{html.escape(it["link"], quote=True)}" style="font:700 18px/1.35 {SERIF};color:{INK};text-decoration:none;">{html.escape(it["subject"])}</a>
        <div class="muted" style="font:400 13px/1.4 {SANS};color:{DIM};padding-top:4px;">{dot}{html.escape(it["sender"])} &nbsp;·&nbsp; {html.escape(it["account"])}</div>{_due_line(it)}
        <p style="font:400 15px/1.55 {SERIF};color:{INK};margin:8px 0 0 0;">{html.escape(it["note"])}</p>{_draft_block(it["draft"], bool(it.get("draft_full")))}
      </td></tr>"""
    return out


def _section(heading: str, rows: str) -> str:
    return f"""
    <tr><td style="padding:26px 0 4px 0;">
      <div style="font:700 13px/1 {SANS};letter-spacing:.1em;color:{INK};text-transform:uppercase;">{html.escape(heading)}</div>
    </td></tr>
    <tr><td style="padding-top:14px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>"""


def _carried_rows(items: list[Triaged], start: int, nag_after_days: int) -> str:
    out = ""
    for n, it in enumerate(items, start):
        dot = "&#9679; " if it["unread"] else ""
        days = waiting_days(it["date"])
        nag = days >= nag_after_days
        badge = (
            f' <span style="font:700 11px/1 {SANS};letter-spacing:.06em;color:{PAPER};background:{INK};padding:3px 6px;border-radius:3px;text-transform:uppercase;">still open</span>'
            if nag
            else ""
        )
        weight = 900 if nag else 700
        out += f"""
      <tr><td style="padding:0 0 26px 0;">
        {_number(n, it["link"])}<a href="{html.escape(it["link"], quote=True)}" style="font:{weight} 18px/1.35 {SERIF};color:{INK};text-decoration:none;">{html.escape(it["subject"])}</a>{badge}
        <div class="muted" style="font:{"700" if nag else "400"} 13px/1.4 {SANS};color:{DIM};padding-top:4px;">{dot}{html.escape(it["sender"])} &nbsp;·&nbsp; {html.escape(it["account"])} &nbsp;·&nbsp; waiting {days} day{"" if days == 1 else "s"}</div>
      </td></tr>"""
    return out


def _carried_footer(cfg: Config) -> str:
    return f"""
    <tr><td class="muted" style="font:400 12px/1.5 {SANS};color:{DIM};padding-top:2px;">Clears when you reply, archive, or remove the {html.escape(cfg.label)} label in Gmail.</td></tr>"""


COMMANDS_HINT = (
    'Reply to this email with e.g. "done 2", "snooze 3 for a week", "draft 1 shorter", "never 4" or "vip 5" '
    "— or label a message mailtriage/done, mailtriage/snooze-3d (1w, 2w), mailtriage/never or mailtriage/vip "
    "in Gmail and the next run acts on it."
)


def email_html(cfg: Config, triaged: list[Triaged], today: date | None = None) -> str:
    needs_action = [t for t in triaged if t["bucket"] == "needs_action"]
    worth_reading = [t for t in triaged if t["bucket"] == "worth_reading"]
    today = today or datetime.now(local_zone(cfg.timezone)).date()
    sections, n = "", 1
    # Carried debts sit right after Needs action, before Worth reading --
    # they're the oldest items in the digest, closest to the top on purpose.
    for kind, heading, items in digest_groups(triaged, today):
        if kind == "carried":
            sections += _section(heading, _carried_rows(items, n, cfg.nag_after_days)) + _carried_footer(cfg)
        else:
            sections += _section(heading, _rows(items, n))
        n += len(items)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<style>@media (prefers-color-scheme:dark){{
  body,.sheet,.sheet table{{background:#111114!important}}
  .sheet a,.sheet p,.sheet div{{color:#eceae5!important}}
  .muted,.muted *{{color:#9a9aa4!important}}
}}</style></head>
<body class="sheet" style="margin:0;padding:0;background:{PAPER};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER};">
<tr><td align="center" style="padding:32px 16px 44px 16px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;">
    <tr><td style="padding-bottom:26px;border-bottom:1px solid {RULE};">
      <div style="font:700 26px/1 {SERIF};color:{INK};">{html.escape(cfg.subject_prefix)}</div>
      <div class="muted" style="font:400 13px/1 {SANS};color:{DIM};padding-top:9px;">{len(needs_action)} to act · {len(worth_reading)} to read</div>
    </td></tr>
    {sections}
    <tr><td class="muted" style="border-top:1px solid {RULE};padding-top:18px;font:400 12px/1.5 {SANS};color:{DIM};">Triaged by mailtriage from your own inboxes. {html.escape(COMMANDS_HINT)}</td></tr>
  </table>
</td></tr></table></body></html>"""


def _week_open_rows(items: list[WeekItem]) -> str:
    out = ""
    for it in sorted(items, key=lambda i: i["date"]):  # oldest first
        out += f"""
      <tr><td style="padding:0 0 14px 0;">
        <a href="{html.escape(it["link"], quote=True)}" style="font:700 15px/1.4 {SERIF};color:{INK};text-decoration:none;">{html.escape(it["subject"])}</a>
        <div class="muted" style="font:400 12px/1.4 {SANS};color:{DIM};padding-top:2px;">{html.escape(it["sender"])} &nbsp;·&nbsp; {it["age_days"]}d</div>
      </td></tr>"""
    return out


def _week_recent_subjects(items: list[WeekItem], limit: int = 5) -> str:
    recent = sorted(items, key=lambda i: i["date"], reverse=True)[:limit]
    lis = "".join(f'<li style="padding:2px 0;">{html.escape(it["subject"])}</li>' for it in recent)
    return f'<ul style="margin:6px 0 0 0;padding-left:18px;font:400 13px/1.5 {SANS};color:{DIM};">{lis}</ul>'


def _week_extra_section(heading: str, items: list[WeekItem]) -> str:
    if not items:
        return ""
    return f"""
    <tr><td class="muted" style="font:700 12px/1.4 {SANS};color:{DIM};padding-top:10px;">{html.escape(heading)} ({len(items)})</td></tr>
    <tr><td>{_week_recent_subjects(items)}</td></tr>"""


def _week_account_block(account: str, buckets: dict[str, list[WeekItem]]) -> str:
    replied, archived, open_items = buckets["replied"], buckets["archived"], buckets["open"]
    open_rows = (
        _week_open_rows(open_items)
        if open_items
        else f'<tr><td class="muted" style="font:400 13px/1.5 {SANS};color:{DIM};padding:2px 0;">Nothing open.</td></tr>'
    )
    return f"""
    <tr><td style="padding:26px 0 4px 0;">
      <div style="font:700 13px/1 {SANS};letter-spacing:.08em;color:{INK};text-transform:uppercase;">{html.escape(account)}</div>
      <div class="muted" style="font:400 12px/1.4 {SANS};color:{DIM};padding-top:6px;">{len(replied)} replied &nbsp;·&nbsp; {len(archived)} archived &nbsp;·&nbsp; {len(open_items)} open</div>
    </td></tr>
    <tr><td style="padding-top:12px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{open_rows}</table></td></tr>
    {_week_extra_section("Replied", replied)}
    {_week_extra_section("Archived", archived)}"""


def weekly_html(cfg: Config, week: WeekResult, done_count: int = 0) -> str:
    handled = sum(len(b["replied"]) + len(b["archived"]) for b in week["accounts"].values())
    still_open = sum(len(b["open"]) for b in week["accounts"].values())
    blocks = "".join(_week_account_block(account, buckets) for account, buckets in week["accounts"].items())
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<style>@media (prefers-color-scheme:dark){{
  body,.sheet,.sheet table{{background:#111114!important}}
  .sheet a,.sheet p,.sheet div{{color:#eceae5!important}}
  .muted,.muted *{{color:#9a9aa4!important}}
}}</style></head>
<body class="sheet" style="margin:0;padding:0;background:{PAPER};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER};">
<tr><td align="center" style="padding:32px 16px 44px 16px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;">
    <tr><td style="padding-bottom:26px;border-bottom:1px solid {RULE};">
      <div style="font:700 26px/1 {SERIF};color:{INK};">Your week</div>
      <div class="muted" style="font:400 13px/1 {SANS};color:{DIM};padding-top:9px;">{handled} handled{f" · {done_count} marked done" if done_count else ""} · {still_open} still open</div>
    </td></tr>
    {blocks}
    <tr><td class="muted" style="border-top:1px solid {RULE};padding-top:18px;font:400 12px/1.5 {SANS};color:{DIM};">Open items clear when you reply, archive, or remove the {html.escape(cfg.label)} label.</td></tr>
  </table>
</td></tr></table></body></html>"""


def _send(cfg: Config, subject: str, text: str, html_body: str | None) -> None:
    """Post to Resend. Shared transport for the normal digest (`send`) and
    the weekly review (delivery.send_html -> `send_html`), so the
    auth/validation/HTTP logic lives in exactly one place. Always carries a
    plain-text part; the HTML part is skipped for `digest_format: text`."""
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        raise MailError(
            "RESEND_API_KEY is not set. Make a key at https://resend.com/api-keys, then add it in your fork: "
            "Settings -> Secrets and variables -> Actions -> New repository secret, named RESEND_API_KEY."
        )
    to, sender = cfg.email_to.strip(), cfg.email_from.strip()
    if not to:
        raise MailError("email_to is empty in config.yaml. Put the address you want the digest delivered to there.")
    if not sender:
        raise MailError(
            "email_from is empty in config.yaml. It must be an address on a domain you verified at "
            "https://resend.com/domains, e.g. mailtriage@yourdomain.com."
        )
    try:
        status, body = post_json(
            "https://api.resend.com/emails",
            {
                "from": sender,
                "to": [to],  # must be a list — a bare string 422s
                "subject": subject,
                "text": text,
                **({"html": html_body} if html_body else {}),
            },
            {"Authorization": f"Bearer {key}"},
        )
    except Exception as e:
        raise MailError(
            f"could not reach api.resend.com ({type(e).__name__}: {e}). "
            "Re-run it with Actions -> triage -> Run workflow."
        ) from e
    if status >= 300:
        raise MailError(
            f"Resend refused the email (HTTP {status}): {body}\n"
            f"  A 403 here almost always means '{sender}' is not on a verified domain — it reads like a bad "
            "API key but it isn't. Verify the sending domain at https://resend.com/domains."
        )


def digest_subject(cfg: Config, triaged: list[Triaged], stamp: str = "") -> str:
    """`stamp` is the slot a scheduled run is for ("Thu 03 Sep 08:00"); it
    sits right after the prefix so imap_pull.already_delivered can search
    for exactly "<prefix> · <stamp>" next time. Manual runs pass none."""
    a = sum(t["bucket"] == "needs_action" for t in triaged)
    r = sum(t["bucket"] == "worth_reading" for t in triaged)
    head = f"{cfg.subject_prefix} · {stamp}" if stamp else cfg.subject_prefix
    return f"{head} · {a} to act · {r} to read"


def send_html(cfg: Config, subject: str, html_body: str) -> None:
    _send(cfg, subject, html_to_text(html_body), html_body)


def send(cfg: Config, triaged: list[Triaged], stamp: str = "") -> None:
    html_body = None if cfg.digest_format == "text" else email_html(cfg, triaged)
    _send(cfg, digest_subject(cfg, triaged, stamp), digest_text(triaged), html_body)
