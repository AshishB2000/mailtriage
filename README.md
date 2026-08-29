# mailtriage

A twice-daily private digest of several Gmail inboxes. A scheduled cloud agent
reads your accounts, sorts every message into **needs-action / worth-reading /
noise**, and republishes one private dashboard to a fixed URL you open on your
phone.

No server, no database, no local state. The time window is the dedupe. Runtime
is **stdlib only** — nothing to install to run it.

## Layout

```
src/mailtriage/imap_pull.py   the engine: pulls recent INBOX mail from every
                              account as JSON (read-only, per-account failure
                              warnings, undated/future-stamped drop)
tests/test_imap_pull.py       the test suite
docs/ROUTINE.md               the cloud-agent run prompt (pull → triage → publish)
docs/SETUP.md                 one-time credential setup (2-Step Verification + app passwords)
```

## Run it

Zero-install (stdlib only):

```bash
python src/mailtriage/imap_pull.py --self-check      # no network, no credentials
python src/mailtriage/imap_pull.py --window-hours 13 # prints the triage JSON
```

Or install the `mailtriage` command:

```bash
pip install -e .
mailtriage --self-check
mailtriage --window-hours 13
```

Set your accounts and app passwords first — see [docs/SETUP.md](docs/SETUP.md):

```
MAIL_ACCOUNTS=alice@gmail.com,alice.work@gmail.com
MAIL_PW_ALICE_GMAIL_COM=…
MAIL_PW_ALICE_WORK_GMAIL_COM=…
```

## Dev

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```
