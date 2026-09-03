"""Command line entry point. The only place that prints to the user or exits."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from mailtriage import __version__
from mailtriage.config import Config, load_config
from mailtriage.errors import MailError
from mailtriage.imap_pull import (
    already_delivered,
    check_login,
    label_actions,
    pull,
    pull_open_actions,
    pull_week,
    push_drafts,
)
from mailtriage.models import Email, Triaged, WeekResult
from mailtriage.schedule import current_slot, due, local_zone


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mailtriage",
        description="Private AI triage of your Gmail inboxes on your schedule, delivered as an HTML email.",
    )
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="triage as normal (the API call still happens) and print the digest instead of sending it",
    )
    ap.add_argument("--self-check", action="store_true", help="run the built-in assertions and exit")
    ap.add_argument(
        "--weekly",
        action="store_true",
        help="send the weekly review (what got handled, what's still open) instead of a normal digest",
    )
    ap.add_argument(
        "--due",
        action="store_true",
        help=(
            "evaluate the schedule only, no network: exit 0 and print the due mode "
            "('digest' or 'weekly') to stdout if a run_at/weekly_review slot is due "
            "this hour, exit 3 if not (never 1, so a real failure is never confused "
            "with 'not due')"
        ),
    )
    # Not argparse's built-in action="version": that calls sys.exit() from inside
    # parse_args(), before main() gets a chance to return normally. Handled below
    # instead, so main() stays the only place that prints and exits.
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    return ap


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_slot(cfg: Config, now_local: datetime) -> str:
    now_minutes = now_local.hour * 60 + now_local.minute
    ordered = sorted(cfg.run_at, key=lambda s: int(s[:2]) * 60 + int(s[3:5]))
    for slot in ordered:
        if int(slot[:2]) * 60 + int(slot[3:5]) > now_minutes:
            return slot
    return ordered[0]  # nothing left today -- wraps to tomorrow's first slot


def _handle_due(cfg: Config, now: datetime, event: str) -> int:
    """Print the due mode ('digest'/'weekly') to STDOUT and return the
    exit-code contract (0 due / 3 not due). All human-readable status lines
    go to stderr, so a caller can capture stdout as a clean mode string --
    see the "Is it time?" step in .github/workflows/digest.yml, which
    captures this to pick which of `mailtriage`/`mailtriage --weekly` to
    run next."""
    result = due(cfg, now, event)
    if result == "digest":
        print("digest")
        return 0
    if result == "weekly":
        print("mailtriage: weekly review slot — running.", file=sys.stderr)
        print("weekly")
        return 0
    now_local = now.astimezone(local_zone(cfg.timezone))
    print(
        f"mailtriage: not due at {now_local:%H:%M} ({cfg.timezone}); "
        f"next slot {_next_slot(cfg, now_local)} — skipping.",
        file=sys.stderr,
    )
    return 3


def _print_digest(kept: list[Triaged]) -> None:
    headings = (
        ("Needs action", "needs_action"),
        ("Still waiting on you", "carried"),
        ("Worth reading", "worth_reading"),
    )
    for heading, bucket in headings:
        items = [t for t in kept if t["bucket"] == bucket]
        if not items:
            continue
        print(heading)
        for it in items:
            line = f"  {it['subject']} · {it['sender']}"
            if it["note"]:
                line += f" · {it['note']}"
            print(line)
            if it["draft"]:
                print(f"    Draft reply: {it['draft']}")


def _print_weekly(week: WeekResult) -> None:
    for account, buckets in week["accounts"].items():
        replied, archived, open_items = buckets["replied"], buckets["archived"], buckets["open"]
        print(f"{account} — {len(replied)} replied · {len(archived)} archived · {len(open_items)} open")
        for it in sorted(open_items, key=lambda i: i["date"]):  # oldest first
            print(f"  {it['subject']} · {it['sender']} · {it['age_days']}d")


def _week_counts(week: WeekResult) -> tuple[int, int]:
    handled = sum(len(b["replied"]) + len(b["archived"]) for b in week["accounts"].values())
    still_open = sum(len(b["open"]) for b in week["accounts"].values())
    return handled, still_open


def run_weekly(cfg: Config, dry_run: bool = False) -> None:
    # Imported here, not at module scope: mirrors run()'s lazy delivery
    # import (weekly_html lives in delivery.mail, alongside the Resend
    # HTTP client) so --self-check keeps working the same way.
    from mailtriage.delivery import send_html
    from mailtriage.delivery.mail import weekly_html

    now = _now()
    week = pull_week(os.environ, now, cfg.label)
def _slot_stamp(cfg: Config, now: datetime, dry_run: bool) -> str:
    """The slot this scheduled run is for, e.g. "Thu 03 Sep 08:00" -- it goes
    in the subject and is what the no-double-send guard searches for. "" for
    a dry run or a manual run (GITHUB_EVENT_NAME=workflow_dispatch or unset),
    which are never stamped and never guarded."""
    if dry_run:
        return ""
    slot = current_slot(cfg, now, os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"))
    return f"{slot:%a %d %b %H:%M}" if slot else ""


def _slot_already_delivered(cfg: Config, stamp: str, now: datetime) -> bool:
    """Two hourly cron firings can both land inside one slot's catch_up_minutes
    window. Gmail is the memory: if any account already holds this slot's
    stamped subject, this run has nothing to do. Runs before pull/triage so
    the duplicate costs no API call either."""
    if stamp and already_delivered(os.environ, cfg.subject_prefix, stamp, now):
        print("mailtriage: this slot's digest was already delivered — sending nothing.", file=sys.stderr)
        return True
    return False



    for w in week["warnings"]:
        print(f"mailtriage: account failed, skipping: {w}", file=sys.stderr)

    handled, still_open = _week_counts(week)
    if handled == 0 and still_open == 0:
        # Same "send nothing" philosophy as the normal digest: a roll-up with
        # nothing to report trains you to ignore it.
    stamp = _slot_stamp(cfg, now, dry_run)
    if _slot_already_delivered(cfg, stamp, now):
        return
        print("mailtriage: nothing this week — sending nothing.", file=sys.stderr)
        return

    if dry_run:
        _print_weekly(week)
        return

    head = f"{cfg.subject_prefix} · {stamp}" if stamp else cfg.subject_prefix
    send_html(cfg, f"{head} · weekly review", weekly_html(cfg, week))
    print(
        f"mailtriage: weekly review delivered ({handled} handled, {still_open} open) via {cfg.delivery}.",
        file=sys.stderr,
    )


def _carried_triaged(em: Email) -> Triaged:
    # idx=-1: carried items have no source index in this run's `emails` list
    # (they were pulled from Gmail by label, not by triage()) -- they never
    # go through drafts or the needs_action rules, both of which key on idx.
    return {
        "bucket": "carried",
        "note": "",
        "account": em["account"],
        "sender": em["from"],
        "subject": em["subject"],
        "link": em["link"],
        "date": em["date"],
        "unread": em["unread"],
        "idx": -1,
        "draft": "",
    }


def run(cfg: Config, dry_run: bool = False) -> None:
    # Imported here, not at module scope: --self-check must work on a machine
    # where `anthropic` failed to install, and this is the only path that needs it.
    from mailtriage.delivery import send
    from mailtriage.drafts import generate_drafts
    from mailtriage.rules import apply_ignore, enforce
    from mailtriage.triage import select_backend, triage

    now = _now()
    result = pull(os.environ, now, cfg.window_hours)

    # A dead account must not fail the run: a red X for one broken account
    # trains you to ignore red X's. Report and carry on with the rest.
    for w in result["warnings"]:
        print(f"mailtriage: account failed, skipping: {w}", file=sys.stderr)

    emails = result["messages"]
    # Counts only -- never subjects or senders. Actions logs on a public fork
    stamp = _slot_stamp(cfg, now, dry_run)
    if _slot_already_delivered(cfg, stamp, now):
        return
    # are public; this line is what lets someone debug "kept none" without
    # leaking what was in the inbox.
    n_accounts = len({e["account"] for e in emails})
    print(
        f"mailtriage: {len(emails)} candidate(s) in the last {cfg.window_hours}h across {n_accounts} account(s).",
        file=sys.stderr,
    )
    before = len(emails)
    emails = apply_ignore(cfg, emails)
    if before > len(emails):
        print(f"mailtriage: rules.always_ignore dropped {before - len(emails)} message(s).", file=sys.stderr)

    if not emails:
        print("mailtriage: nothing recent — sending nothing.", file=sys.stderr)
        return

    kept = triage(cfg, emails, now)
    kept = enforce(cfg, emails, kept)  # rule-forced items must survive even if the model returned none
    if not kept:
        # Carried items alone must never trigger a digest: this check runs
        # before any carry-over items are merged in, so "no new items" stays
        # "no new items" even when yesterday's debts are still open.
        print("mailtriage: the model kept none of the candidates — sending nothing.", file=sys.stderr)
        return

    if cfg.carry_over:
        if not dry_run:
            # No mailbox writes on a dry run: label only when actually delivering.
            for w in label_actions(os.environ, kept, emails, cfg.label):
                print(f"mailtriage: label failed, skipping: {w}", file=sys.stderr)

        # Reading is fine on a dry run -- only writes are skipped above.
        carried = pull_open_actions(os.environ, now, cfg.window_hours, cfg.label)
        for w in carried["warnings"]:
            print(f"mailtriage: carried-mail lookup failed, skipping: {w}", file=sys.stderr)
        kept = kept + [_carried_triaged(em) for em in carried["messages"]]

    if cfg.draft_replies and any(t["bucket"] == "needs_action" for t in kept):
        # select_backend is pure env inspection -- picking again here (instead
        # of threading a pick through triage()) keeps triage()'s signature
        # unchanged and costs nothing extra.
        _name, call = select_backend(cfg, os.environ)
        generate_drafts(cfg, call, emails, kept)  # MailError here is fatal -- auth is auth.
        if not dry_run:
            # No mailbox writes on a dry run: push only when actually delivering.
            for w in push_drafts(os.environ, kept, emails):
                print(f"mailtriage: draft push failed, skipping: {w}", file=sys.stderr)

    if dry_run:
        _print_digest(kept)
        return

    send(cfg, kept, stamp)
    print(f"mailtriage: delivered {len(kept)} item(s) via {cfg.delivery}.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.version:
        print(f"mailtriage {__version__}")
        return 0

    try:
        if args.self_check:
            from mailtriage.selfcheck import self_check

            self_check()
            return 0
        if args.due:
            event = os.environ.get("GITHUB_EVENT_NAME", "schedule")
            return _handle_due(load_config(args.config), _now(), event)
        if args.weekly:
            run_weekly(load_config(args.config), dry_run=args.dry_run)
        else:
            run(load_config(args.config), dry_run=args.dry_run)
    except MailError as e:
        print(f"mailtriage: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
