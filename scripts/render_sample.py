"""Render docs/sample-digest.html -- the static digest preview the setup wizard
shows before the first run. Rendered through the real template
(delivery.mail.email_html) from a fixed fixture so the preview can never drift
from what a fork actually sends; tests/test_sample.py fails when it does.

    .venv/bin/python scripts/render_sample.py   # rewrites docs/sample-digest.html
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from mailtriage.config import Config
from mailtriage.delivery.mail import email_html
from mailtriage.models import Triaged

OUT = Path(__file__).resolve().parent.parent / "docs" / "sample-digest.html"


def _item(bucket: str, sender: str, subject: str, note: str, account: str, **extra: object) -> Triaged:
    base = {
        "bucket": bucket,
        "note": note,
        "account": account,
        "sender": sender,
        "subject": subject,
        "link": "https://mail.google.com/mail/u/0/#inbox",
        # A carried item prints its age relative to now, so the date is
        # anchored to "3 days ago" at render time -- the output stays stable.
        "date": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
        "unread": False,
        "idx": 0,
        "draft": "",
    }
    return cast(Triaged, {**base, **extra})


def render() -> str:
    cfg = Config.from_mapping({"delivery": "gmail"})
    triaged = [
        _item(
            "needs_action",
            "Priya Shah",
            "Q3 budget sign-off — due Friday",
            "She needs your approval on the revised number by EOD Friday to hit the finance deadline.",
            "alice.work@gmail.com",
            unread=True,
            draft=(
                "Hi Priya, the revised number looks good to me — approved. "
                "Let me know if you need anything else before Friday.\nThanks,"
            ),
        ),
        _item(
            "needs_action",
            "United Airlines",
            "Flight change confirmation required",
            "Your itinerary changed — confirm or rebook before the fare hold expires tonight.",
            "alice@gmail.com",
        ),
        _item(
            "carried",
            "Dana Ortiz",
            "Reference letter for Sam?",
            "",
            "alice@gmail.com",
        ),
        _item(
            "worth_reading",
            "Simon Willison's Weblog",
            "Running local models on 8GB",
            "First quantized result that actually fits the memory budget you keep hitting.",
            "alice@gmail.com",
        ),
        _item(
            "worth_reading",
            "Jordan Lee",
            "Team retro notes from Tuesday",
            "Worth a skim before next sprint planning.",
            "alice.work@gmail.com",
        ),
    ]
    return email_html(cfg, triaged)


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT}")
