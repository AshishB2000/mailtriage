"""A synthetic inbox with a known right answer, for measuring the prompt.

The triage prompt is the product, and until this file existed there was no
way to change it except by shipping it and watching a real inbox. That is a
terrible feedback loop: a run against real mail cannot be pasted into a
public Actions log, cannot be repeated with the same input, and has no
correct answer to compare against.

Every message here is invented. Nothing in this file is anyone's mail, so
`--bench` can print its whole verdict table to a public log.

The mix is drawn from what a normal personal Gmail window actually holds:
mostly automated noise, a handful of real obligations, a couple of things
worth a glance. Half the needs_action items are deliberately the hard kind
-- an obligation wearing an automated message's clothes -- because those are
the ones a "when in doubt, leave it out" reading of the prompt drops.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from mailtriage.models import Email

# What the model should say about each message. "noise" means: return nothing
# for it at all -- omission is the label, there is no third bucket.
NEEDS_ACTION = "needs_action"
WORTH_READING = "worth_reading"
NOISE = "noise"

# (minutes ago, from, subject, snippet, expected bucket, extras)
_INBOX: list[tuple[int, str, str, str, str, dict[str, Any]]] = [
    (
        35,
        "Priya Raghavan <priya@northwind.example>",
        "Re: Q3 vendor contract — need your sign-off",
        (
            "I've attached the redlined version. Legal wants this back before Friday's close, "
            "so can you look at clause 4 and let me know if you're happy with it?"
        ),
        NEEDS_ACTION,
        {"attachments": ["vendor-contract-v3.pdf (application/pdf)"], "replied_before": 14},
    ),
    (
        70,
        "billing@figma.com",
        "Your card ending 4471 will expire before your next renewal",
        (
            "We couldn't validate the card on file for your annual plan renewing on the 18th. "
            "Update your payment method to avoid an interruption."
        ),
        NEEDS_ACTION,
        {},
    ),
    (
        95,
        "DocuSign <dse@docusign.net>",
        "Please DocuSign: Rental_Agreement_2027.pdf",
        "Marcus Bell sent you a document to review and sign.",
        NEEDS_ACTION,
        {"attachments": ["Rental_Agreement_2027.pdf (application/pdf)"]},
    ),
    (
        130,
        "Dr. Osei's office <frontdesk@bridgedental.example>",
        "Confirm your appointment on Thursday 10:40am",
        (
            "Reply YES to confirm or call us on 555-0138. Appointments not confirmed 24 hours "
            "ahead are released to the waiting list."
        ),
        NEEDS_ACTION,
        {},
    ),
    (
        180,
        "Tomas Lindqvist <tomas@lindqvist.example>",
        "did you get a chance to look at the deck?",
        (
            "No rush, but I need to send it to the board Monday morning and I'd rather it went "
            "with your numbers in it than without."
        ),
        NEEDS_ACTION,
        {"replied_before": 31, "thread": ["4d ago · you: Will go through it this week."]},
    ),
    (
        240,
        "no-reply@namecheap.com",
        "hendrikson.dev expires in 6 days",
        (
            "Auto-renew is off for this domain. Domains not renewed before expiry enter a "
            "redemption period with a restoration fee."
        ),
        NEEDS_ACTION,
        {},
    ),
    (
        300,
        "Anika Fowler <a.fowler@meridianhealth.example>",
        "Invitation: Design review — Tue 15:00 (RSVP needed)",
        (
            "Adding you because we'll be going through the onboarding flow you wrote. "
            "Let me know if that time doesn't work and I'll move it."
        ),
        NEEDS_ACTION,
        {"replied_before": 6},
    ),
    (
        420,
        "Sam Okafor <sam@okafor.example>",
        "photos from the weekend",
        (
            "Finally got round to sorting these. The one of you on the ridge came out great — "
            "no need to do anything, just thought you'd want them."
        ),
        WORTH_READING,
        {"replied_before": 22},
    ),
    (
        480,
        "Ruth Delacroix <ruth@parallaxlabs.example>",
        "Where we landed on the migration",
        (
            "Long one, sorry. Summary: we're keeping the old indexer until Q1, the numbers "
            "didn't justify the rewrite. Nothing needed from you, purely FYI."
        ),
        WORTH_READING,
        {"replied_before": 9},
    ),
    (
        540,
        "Ben Attah <ben@attah.example>",
        "that bookshop you mentioned",
        "Went yesterday. The basement is enormous and they had two of the Sebald. Thought of you.",
        WORTH_READING,
        {"replied_before": 40},
    ),
    # ---- noise from here down ----
    (
        20,
        "Uber Receipts <noreply@uber.com>",
        "Your Tuesday afternoon trip with Uber",
        "Thanks for riding. Total £14.20 charged to your card ending 4471.",
        NOISE,
        {},
    ),
    (
        45,
        "Amazon.co.uk <shipment-tracking@amazon.co.uk>",
        "Your package has been delivered",
        "Your parcel was left in a safe place: porch. Track your package for details.",
        NOISE,
        {},
    ),
    (
        60,
        "GitHub <notifications@github.com>",
        "[hendrikson/atlas] Run failed: nightly build (main)",
        "The workflow run 'nightly build' failed for commit 3f9c1e2.",
        NOISE,
        {},
    ),
    (
        85,
        "The Browser <hello@thebrowser.com>",
        "Five articles for a Tuesday",
        "A physicist on why time is not what you think, plus the strange afterlife of the cassette tape.",
        NOISE,
        {"unsubscribe": "https://thebrowser.example/unsubscribe/abc"},
    ),
    (
        110,
        "LinkedIn <messages-noreply@linkedin.com>",
        "You appeared in 9 searches this week",
        "See who's been looking at your profile.",
        NOISE,
        {"unsubscribe": "https://linkedin.example/unsub"},
    ),
    (
        150,
        "MADE.COM <news@made.example>",
        "48 HOURS ONLY: up to 60% off everything ⚡ ACT NOW",
        (
            "Your basket is waiting and this offer expires at midnight. Don't miss out — "
            "action required to secure your discount."
        ),
        NOISE,
        {"unsubscribe": "https://made.example/unsub"},
    ),
    (
        200,
        "Strava <no-reply@strava.com>",
        "Marta gave you kudos",
        "Marta Kowalczyk gave kudos to your morning run.",
        NOISE,
        {"unsubscribe": "https://strava.example/unsub"},
    ),
    (
        260,
        "Monzo <hello@monzo.com>",
        "Your monthly spending summary is ready",
        "You spent £1,204 in August, 8% less than July.",
        NOISE,
        {},
    ),
    (
        320,
        "Sentry <noreply@sentry.io>",
        "[atlas] TimeoutError in worker.dispatch (12 events)",
        "This issue has been seen 12 times in the last hour.",
        NOISE,
        {},
    ),
    (
        360,
        "Substack <no-reply@substack.com>",
        "New from Ana Vieira: The quiet part of the housing numbers",
        "Read the full post on Substack.",
        NOISE,
        {"unsubscribe": "https://substack.example/unsub"},
    ),
    (
        400,
        "Trainline <noreply@thetrainline.com>",
        "Your receipt for London → Manchester",
        "Booking reference JKD8821. Total £47.50.",
        NOISE,
        {},
    ),
    (
        450,
        "Duolingo <hello@duolingo.com>",
        "You're on a 3 day streak! 🔥",
        "Keep it going — practise now to protect your streak.",
        NOISE,
        {"unsubscribe": "https://duolingo.example/unsub"},
    ),
    (
        500,
        "Apple <no_reply@email.apple.com>",
        "Your subscription to iCloud+ renews on 11 September",
        "200GB storage, £2.99/month. No action is needed; this is a reminder.",
        NOISE,
        {},
    ),
    (
        560,
        "Slack <feedback@slack.com>",
        "How was your experience with Slack support?",
        "Take our 2 minute survey and tell us how we did.",
        NOISE,
        {"unsubscribe": "https://slack.example/unsub"},
    ),
]


def bench_inbox(now: datetime | None = None) -> tuple[list[Email], list[str]]:
    """The synthetic window and the bucket each message belongs in.

    Returned as two parallel lists so index i of the emails is index i of the
    expectations -- the same integer the model is asked to copy back.
    """
    base = now or datetime.now(timezone.utc)
    emails: list[Email] = []
    expected: list[str] = []
    for minutes, sender, subject, snippet, bucket, extras in _INBOX:
        em: Email = {
            "account": "reader@example.com",
            "from": sender,
            "subject": subject,
            "snippet": snippet,
            "body": snippet,
            "date": (base - timedelta(minutes=minutes)).isoformat(),
            "unread": True,
            "link": "",
            "message_id": f"<bench-{minutes}@example.com>",
            "reply_to": sender,
            "uid": "",
        }
        em.update(extras)  # type: ignore[typeddict-item]
        emails.append(em)
        expected.append(bucket)
    return emails, expected


def score(expected: list[str], got_buckets: dict[int, str]) -> dict[str, int]:
    """Compare the model's verdict to the known answer.

    `missed` is the number that matters: an obligation the reader would never
    have heard about. `false_action` guards the other direction, so a prompt
    cannot score well by calling everything an action item.
    """
    missed = sum(1 for i, want in enumerate(expected) if want == NEEDS_ACTION and got_buckets.get(i) != NEEDS_ACTION)
    found = sum(1 for i, want in enumerate(expected) if want == NEEDS_ACTION and got_buckets.get(i) == NEEDS_ACTION)
    false_action = sum(1 for i, want in enumerate(expected) if want == NOISE and got_buckets.get(i) == NEEDS_ACTION)
    noise_returned = sum(1 for i, want in enumerate(expected) if want == NOISE and i in got_buckets)
    reading_found = sum(1 for i, want in enumerate(expected) if want == WORTH_READING and got_buckets.get(i))
    return {
        "actions": sum(1 for w in expected if w == NEEDS_ACTION),
        "found": found,
        "missed": missed,
        "false_action": false_action,
        "noise_returned": noise_returned,
        "reading_found": reading_found,
        "reading_total": sum(1 for w in expected if w == WORTH_READING),
    }
