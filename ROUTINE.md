# Mail triage run

Each fire, do exactly this:

1. Run: `python mailtriage/imap_pull.py --window-hours 13`
   Read the JSON on stdout: `{"messages": [...], "warnings": [...]}`.

2. Triage EVERY message into one bucket by reading its `from`, `subject`,
   and `snippet`. Judgement, not keyword rules:
   - 🔴 **Needs action** — the user must DO something: reply expected, a bill /
     payment, a deadline or RSVP, an expiring item, a direct question to them.
   - 🟡 **Worth reading** — a real human or real content worth a glance, but no
     action required.
   - ⚪ **Noise** — newsletters, promotions, receipts, automated notifications.

3. Report the TRUE picture, never pad. If 🔴 is empty, say "nothing needs you" —
   do not promote 🟡 items to fill it. Returning fewer is correct.

4. Write ONE short summary line per 🔴 and 🟡 item (what it is + why it matters).
   Collapse ⚪ into a single rollup line with counts ("14 newsletters, 3 receipts"),
   never one row each.

5. Build a **private artifact dashboard** and republish it to the SAME url every
   run (the pinned url is below). Layout:
   - Header: "As of <local time>", per-bucket counts.
   - If `warnings` is non-empty, a banner: which accounts failed to load.
   - 🔴 section, then 🟡 section — full rows, newest/most-urgent first.
     Each row: account chip · sender · one-line summary · an "open" link (`link`).
   - ⚪ section — the collapsed rollup line, expandable.
   - Per-account filter (chips) plus a combined "all accounts" view.
   - Honest empty state ("Quiet inbox — nothing needs you"), no filler.
   - Theme-aware, mobile-first (read on a phone).
   - Favicon: 📬. Keep it stable across runs.

## Pinned dashboard URL

<!-- Filled in after the dashboard is first published. One URL, reused every run. -->

## Standing corrections

<!-- Plain-English notes the user adds over time, e.g.
     "Mail from noreply@bank.com about statements is 🔴, not noise."
     Read and honor these during step 2. -->
