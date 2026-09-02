"""Command line entry point. The only place that prints to the user or exits."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from mailtriage import __version__
from mailtriage.config import Config, load_config
from mailtriage.errors import MailError
from mailtriage.imap_pull import pull, push_drafts
from mailtriage.models import Triaged
from mailtriage.schedule import due, local_zone


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
        "--due",
        action="store_true",
        help=(
            "evaluate the schedule only, no network: exit 0 if a run_at/weekly_review "
            "slot is due this hour, exit 3 if not (never 1, so a real failure is never "
            "confused with 'not due')"
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
    result = due(cfg, now, event)
    if result == "digest":
        return 0
    if result == "weekly":
        # The weekly digest's own send path is a later PR -- for now the slot
        # is recognized but does no work, same as "not due".
        print("mailtriage: weekly review slot (not implemented yet) — skipping.", file=sys.stderr)
        return 3
    now_local = now.astimezone(local_zone(cfg.timezone))
    print(
        f"mailtriage: not due at {now_local:%H:%M} ({cfg.timezone}); "
        f"next slot {_next_slot(cfg, now_local)} — skipping.",
        file=sys.stderr,
    )
    return 3


def _print_digest(kept: list[Triaged]) -> None:
    for heading, bucket in (("Needs action", "needs_action"), ("Worth reading", "worth_reading")):
        items = [t for t in kept if t["bucket"] == bucket]
        if not items:
            continue
        print(heading)
        for it in items:
            print(f"  {it['subject']} · {it['sender']} · {it['note']}")
            if it["draft"]:
                print(f"    Draft reply: {it['draft']}")


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
        # Delivering "no items today" three times a day is how a reader unsubscribes.
        print("mailtriage: the model kept none of the candidates — sending nothing.", file=sys.stderr)
        return

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

    send(cfg, kept)
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
        run(load_config(args.config), dry_run=args.dry_run)
    except MailError as e:
        print(f"mailtriage: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
