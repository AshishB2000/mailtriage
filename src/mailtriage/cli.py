"""Command line entry point. The only place that prints to the user or exits."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from mailtriage import __version__
from mailtriage.config import Config, load_config
from mailtriage.errors import MailError
from mailtriage.imap_pull import pull
from mailtriage.models import Triaged


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mailtriage",
        description="Twice-daily private AI triage of your Gmail inboxes, delivered as an HTML email.",
    )
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="triage as normal (the API call still happens) and print the digest instead of sending it",
    )
    ap.add_argument("--self-check", action="store_true", help="run the built-in assertions and exit")
    # Not argparse's built-in action="version": that calls sys.exit() from inside
    # parse_args(), before main() gets a chance to return normally. Handled below
    # instead, so main() stays the only place that prints and exits.
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    return ap


def _print_digest(kept: list[Triaged]) -> None:
    for heading, bucket in (("Needs action", "needs_action"), ("Worth reading", "worth_reading")):
        items = [t for t in kept if t["bucket"] == bucket]
        if not items:
            continue
        print(heading)
        for it in items:
            print(f"  {it['subject']} · {it['sender']} · {it['note']}")


def run(cfg: Config, dry_run: bool = False) -> None:
    # Imported here, not at module scope: --self-check must work on a machine
    # where `anthropic` failed to install, and this is the only path that needs it.
    from mailtriage.delivery import send
    from mailtriage.triage import triage

    now = datetime.now(timezone.utc)
    result = pull(os.environ, now, cfg.window_hours)

    # A dead account must not fail the run: a red X for one broken account
    # trains you to ignore red X's. Report and carry on with the rest.
    for w in result["warnings"]:
        print(f"mailtriage: account failed, skipping: {w}", file=sys.stderr)

    emails = result["messages"]
    if not emails:
        print("mailtriage: nothing recent — sending nothing.", file=sys.stderr)
        return

    kept = triage(cfg, emails, now)
    if not kept:
        # Delivering "no items today" three times a day is how a reader unsubscribes.
        print("mailtriage: the model kept none of the candidates — sending nothing.", file=sys.stderr)
        return

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
        run(load_config(args.config), dry_run=args.dry_run)
    except MailError as e:
        print(f"mailtriage: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
