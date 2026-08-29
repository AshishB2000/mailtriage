# mailtriage

A twice-daily private digest of several Gmail inboxes. A scheduled cloud agent
reads your accounts, sorts every message into **needs-action / worth-reading /
noise**, and republishes one private dashboard to a fixed URL you open on your
phone.

No server, no database, no local state. The time window is the dedupe.

## Files

| File | What it is |
|------|-----------|
| `imap_pull.py`     | Stdlib-only engine: pulls the last ~13h from every account's INBOX as JSON. Read-only (never marks mail read), drops undated/future-stamped mail, one bad login becomes a warning instead of blanking the run. |
| `test_imap_pull.py`| The test suite (parser, time window, per-account failure, mixed-timezone sort guard). |
| `ROUTINE.md`       | The prompt the cloud agent follows each run: pull → triage → republish the dashboard. |
| `SETUP.md`         | One-time credential setup (2-Step Verification + app passwords). |

## Quick start

```bash
python imap_pull.py --self-check      # no network, no credentials — proves the engine is intact
```

Then follow `SETUP.md` to create one app password per Gmail account and set:

```
MAIL_ACCOUNTS=alice@gmail.com,alice.work@gmail.com
MAIL_PW_ALICE_GMAIL_COM=…
MAIL_PW_ALICE_WORK_GMAIL_COM=…
```

```bash
python imap_pull.py --window-hours 13   # prints the triage JSON for the last 13h
```

## Dev

```bash
python -m pytest        # run the suite
ruff check . && ruff format --check .
```

Runtime is **stdlib only** — no dependencies to install. `pytest` and `ruff`
are dev-only.
