"""Email delivery via Resend.

Named ``mail`` rather than ``email`` so nothing in this package can shadow the
stdlib ``email`` module for a reader skimming imports.
"""

from __future__ import annotations

import html
import os
from datetime import datetime, timezone

from mailtriage.config import Config
from mailtriage.delivery.http import post_json
from mailtriage.errors import MailError
from mailtriage.models import Triaged

INK, DIM, RULE, PAPER = "#16161a", "#6b6b76", "#e4e2dd", "#faf9f7"
SERIF = "Georgia,'Times New Roman',serif"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"


def _draft_block(draft: str) -> str:
    if not draft:
        return ""
    # white-space:pre-wrap keeps the model's paragraph breaks without a <br>-injection risk.
    return f"""
        <div class="muted" style="margin:10px 0 0 0;padding:10px 12px;border-left:2px solid {RULE};">
          <div style="font:700 11px/1 {SANS};letter-spacing:.08em;color:{DIM};text-transform:uppercase;">Draft reply</div>
          <p style="font:400 14px/1.5 {SANS};color:{DIM};margin:6px 0 0 0;white-space:pre-wrap;">{html.escape(draft)}</p>
        </div>"""


def _rows(items: list[Triaged]) -> str:
    out = ""
    for it in items:
        dot = "&#9679; " if it["unread"] else ""
        out += f"""
      <tr><td style="padding:0 0 26px 0;">
        <a href="{html.escape(it["link"], quote=True)}" style="font:700 18px/1.35 {SERIF};color:{INK};text-decoration:none;">{html.escape(it["subject"])}</a>
        <div class="muted" style="font:400 13px/1.4 {SANS};color:{DIM};padding-top:4px;">{dot}{html.escape(it["sender"])} &nbsp;·&nbsp; {html.escape(it["account"])}</div>
        <p style="font:400 15px/1.55 {SERIF};color:{INK};margin:8px 0 0 0;">{html.escape(it["note"])}</p>{_draft_block(it["draft"])}
      </td></tr>"""
    return out


def _section(heading: str, items: list[Triaged]) -> str:
    if not items:
        return ""
    return f"""
    <tr><td style="padding:26px 0 4px 0;">
      <div style="font:700 13px/1 {SANS};letter-spacing:.1em;color:{INK};text-transform:uppercase;">{html.escape(heading)}</div>
    </td></tr>
    <tr><td style="padding-top:14px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{_rows(items)}</table></td></tr>"""


def _age(date_iso: str) -> str:
    days = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(date_iso)).days)
    return f"{days}d" if days else "<1d"


def _carried_rows(items: list[Triaged]) -> str:
    out = ""
    for it in items:
        dot = "&#9679; " if it["unread"] else ""
        out += f"""
      <tr><td style="padding:0 0 26px 0;">
        <a href="{html.escape(it["link"], quote=True)}" style="font:700 18px/1.35 {SERIF};color:{INK};text-decoration:none;">{html.escape(it["subject"])}</a>
        <div class="muted" style="font:400 13px/1.4 {SANS};color:{DIM};padding-top:4px;">{dot}{html.escape(it["sender"])} &nbsp;·&nbsp; {html.escape(it["account"])} &nbsp;·&nbsp; {html.escape(_age(it["date"]))}</div>
      </td></tr>"""
    return out


def _carried_section(cfg: Config, items: list[Triaged]) -> str:
    if not items:
        return ""
    return f"""
    <tr><td style="padding:26px 0 4px 0;">
      <div style="font:700 13px/1 {SANS};letter-spacing:.1em;color:{INK};text-transform:uppercase;">Still waiting on you</div>
    </td></tr>
    <tr><td style="padding-top:14px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{_carried_rows(items)}</table></td></tr>
    <tr><td class="muted" style="font:400 12px/1.5 {SANS};color:{DIM};padding-top:2px;">Clears when you reply, archive, or remove the {html.escape(cfg.label)} label in Gmail.</td></tr>"""


def email_html(cfg: Config, triaged: list[Triaged]) -> str:
    needs_action = [t for t in triaged if t["bucket"] == "needs_action"]
    carried = [t for t in triaged if t["bucket"] == "carried"]
    worth_reading = [t for t in triaged if t["bucket"] == "worth_reading"]
    # Carried debts sit right after Needs action, before Worth reading --
    # they're the oldest items in the digest, closest to the top on purpose.
    sections = (
        _section("Needs action", needs_action)
        + _carried_section(cfg, carried)
        + _section("Worth reading", worth_reading)
    )
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
    <tr><td class="muted" style="border-top:1px solid {RULE};padding-top:18px;font:400 12px/1.5 {SANS};color:{DIM};">Triaged by mailtriage from your own inboxes.</td></tr>
  </table>
</td></tr></table></body></html>"""


def send(cfg: Config, triaged: list[Triaged]) -> None:
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
    needs_action = [t for t in triaged if t["bucket"] == "needs_action"]
    worth_reading = [t for t in triaged if t["bucket"] == "worth_reading"]
    a, r = len(needs_action), len(worth_reading)
    try:
        status, body = post_json(
            "https://api.resend.com/emails",
            {
                "from": sender,
                "to": [to],  # must be a list — a bare string 422s
                "subject": f"{cfg.subject_prefix} · {a} to act · {r} to read",
                "html": email_html(cfg, triaged),
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
