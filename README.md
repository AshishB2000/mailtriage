# mailtriage

**Twice-daily AI triage of your Gmail inboxes, delivered as one private email.**

mailtriage reads the INBOX of one or more Gmail accounts, sorts what actually
arrived into **needs action** / **worth reading** — and drops the rest —
then sends you a single short HTML email. Everything else is noise it never
shows you: newsletters, receipts, promotions, automated notifications.

It runs on **your** GitHub Actions, with **your** Anthropic API key, reading
**your** Gmail over read-only IMAP. Your laptop can be off. There's no
server, no database, no accounts, and nothing routes through anyone else —
you fork this repo and it becomes entirely yours.

---

## What it looks like

```
mailtriage · 2 to act · 3 to read

NEEDS ACTION

  Q3 budget sign-off — due Friday
  ● Priya Shah · alice.work@gmail.com
  She needs your approval on the revised number by EOD Friday to hit
  the finance deadline.

  Flight change confirmation required
  United Airlines · alice@gmail.com
  Your itinerary changed — confirm or rebook before the fare hold
  expires tonight.

WORTH READING

  Simon Willison on running local models on 8GB
  Simon Willison's Weblog · alice@gmail.com
  First quantized result that actually fits the memory budget you
  keep hitting.

  Team retro notes from Tuesday
  Jordan Lee · alice.work@gmail.com
  Worth a skim before next sprint planning.

Triaged by mailtriage from your own inboxes.
```

**It returns fewer, never pads.** `reading_count` in `config.yaml` is a
*maximum*, not a target — the model is explicitly told an honest short list
beats a padded one, and that leaving out a borderline item makes the digest
strictly better. `needs_action` has no cap at all; nothing that genuinely
needs you gets dropped to keep the list short. On a quiet window the email is
short. If nothing cleared the bar in either bucket, **no email sends at
all** — a digest that shows up with "nothing today" three times a week is
how you train yourself to stop opening it.

<!-- screenshot: the HTML email as rendered in Gmail, light and dark -->

---

## Setup — about 5 minutes

### 1. Fork this repo

Click **Fork**. Everything below happens inside your fork.

### 2. Enable Actions

Open the **Actions** tab of your fork. GitHub disables scheduled workflows
in forks by default, so you'll see a banner — click **"I understand my
workflows, go ahead and enable them."**

<!-- screenshot: Actions tab, enable-workflows banner -->

### 3. Add the secrets

**Settings → Secrets and variables → Actions → New repository secret.**

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | a key from [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| `RESEND_API_KEY` | a key from [resend.com/api-keys](https://resend.com/api-keys) (free tier) — you'll also need to verify a sending domain at [resend.com/domains](https://resend.com/domains) |
| `MAIL_ACCOUNTS` | comma-separated Gmail addresses, e.g. `alice@gmail.com,alice.work@gmail.com` |
| one `MAIL_PW_*` per address | the app password for that address (see step 4) |

**The `MAIL_PW_*` names are the most error-prone step.** Each is
`MAIL_PW_` + the address, upper-cased, with every non-alphanumeric character
turned into `_` — exactly the transform `imap_pull.pw_env_var` runs to look
it up at run time:

```
alice@gmail.com       →  MAIL_PW_ALICE_GMAIL_COM
alice.work@gmail.com  →  MAIL_PW_ALICE_WORK_GMAIL_COM
```

Get the name wrong and that one account is skipped with a warning in the
Actions log — the run still completes for every other account.

### 4. Create an app password per account

For **each** Gmail address in `MAIL_ACCOUNTS`:

1. Enable 2-Step Verification at [myaccount.google.com/security](https://myaccount.google.com/security).
2. Create an app password (choose "Mail") at
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Paste the 16-character value into the matching `MAIL_PW_*` secret above.

Full walkthrough: [docs/SETUP.md](docs/SETUP.md).

### 5. Edit `config.yaml` and commit

Open `config.yaml` in GitHub's web editor (or clone and edit locally) and set
at least `email_to` and `email_from`. `interests`, `avoid`, and
`window_hours` ship with sane defaults but are worth tuning to your own
inbox. Commit the change — `config.yaml` holds no secrets and is meant to be
committed.

### 6. Trigger a first run

**Actions → digest → Run workflow**, or just wait for the next scheduled
run. Check the log if nothing arrives — see Troubleshooting below.

---

## What you'll need, and what it costs

| | Where | Cost |
|---|---|---|
| **Anthropic API key** | [console.anthropic.com](https://console.anthropic.com) | A few cents a month |
| **Resend key** | [resend.com](https://resend.com) | Free tier covers personal volume |
| **GitHub Actions** | already have it | Free minutes are ample for 2 runs/day |

Each run is one API call with your recent INBOX mail in and a short triage
out, on `claude-sonnet-5` — this is headline-scale triage, not a long
conversation. Two runs a day lands in the low single-digit cents per month
for a typical inbox. You're billed directly by Anthropic and by Resend;
nothing goes through this project.

---

## How it works

1. **Pull** — connect to each Gmail account over IMAP, **read-only**
   (`readonly=True`, `BODY.PEEK[]`) — mailtriage never marks anything read.
2. **Window** — keep only messages inside `window_hours`; drop anything
   undated.
3. **Triage** — one Anthropic API call, forced through a tool, with your
   `interests` and `avoid` text and the windowed messages. The model returns
   bucket + one-line note per message it's keeping, referenced by an integer
   index — never a URL, so there's no risk of the model inventing or mangling
   a link.
4. **Send** — one HTML email via Resend, or nothing if both buckets came
   back empty.

**No state, anywhere.** There's no database and no record of what was
already sent — `window_hours` is the *only* dedupe. That means
`window_hours` **must be at least as long as the largest gap between two
consecutive scheduled runs**, or mail that arrives inside the gap is
silently skipped forever and never appears in any digest. The shipped
`config.yaml` (`window_hours: 13`) is tuned to the shipped
`.github/workflows/digest.yml` (12-hour cron spacing, an hour of slack) — if
you change the schedule, update `window_hours` to match.

---

## The 60-day caveat

GitHub automatically disables scheduled workflows on a repo after **60 days
with no activity** at all — not specific to this project, it applies to
every scheduled workflow on every repo. If your digest stops arriving,
that's almost always why. Fix: **Actions** tab → re-enable the workflow, or
just push any commit to reset the clock.

---

## Troubleshooting

Actions tab → open the failed run → read the log. mailtriage's errors are
written to say what's wrong and how to fix it, not just that something
failed.

| Log message | What it means | Fix |
|---|---|---|
| `ANTHROPIC_API_KEY is not set.` | The secret is missing | Add it under Settings → Secrets and variables → Actions |
| `Anthropic rejected ANTHROPIC_API_KEY.` | The secret exists but the key is wrong, revoked, or has a stray space | Generate a fresh key and update the secret |
| `Anthropic rate-limited this run, or the account is out of credit.` | Billing/rate limit on the Anthropic side, not a bug | Check your balance; the next scheduled run picks things up |
| `MAIL_ACCOUNTS is empty` | The secret isn't set, or is blank | Set it to a comma-separated list of Gmail addresses |
| `<addr>: no app password found in $MAIL_PW_...` | That account's `MAIL_PW_*` secret is missing or misnamed | Re-check the name against the transform in step 3 above; this account is skipped, the run continues for the rest |
| `RESEND_API_KEY is not set.` | The secret is missing | Add it from resend.com/api-keys |
| `email_to is empty in config.yaml.` | You haven't set a destination address | Edit `config.yaml` |
| `email_from is empty in config.yaml.` | Same, for the sender address | Edit `config.yaml` — must be on a domain verified with Resend |
| `Resend refused the email (HTTP 403)...` | Almost always an unverified sending domain, **not** a bad API key | Verify the domain at resend.com/domains |
| `could not reach api.resend.com` / `could not reach api.anthropic.com` | The runner had no network, or the API was briefly down | Re-run the workflow by hand |
| `the model's reply was cut off (stop_reason=max_tokens)` | Too much input for the reply budget | Lower `reading_count` in `config.yaml`, or shorten `interests` |
| `mailtriage: account failed, skipping: ...` (per-account warning) | One account's IMAP login failed (bad password, network) | That account is skipped; every other account still triages normally |
| `mailtriage: nothing recent — sending nothing.` | No mail in the window at all | Nothing to fix — normal on a quiet window |
| `mailtriage: the model kept none of the candidates — sending nothing.` | The model triaged everything as noise | Working as intended, not a failure |

---

## Privacy

- Your mail never leaves your infrastructure except to the two services you
  configured yourself: Anthropic (to triage) and Resend (to deliver).
  Nothing routes through the maintainer of this project — no analytics, no
  telemetry, no hosted anything.
- IMAP access is **read-only**: mailtriage selects `INBOX` with
  `readonly=True` and fetches with `BODY.PEEK[]`, so it can never mark a
  message as read or change anything in your mailbox.
- Secrets (API keys, app passwords) live only in your fork's GitHub Actions
  secrets — encrypted at rest by GitHub, not readable back by anyone
  including you, once saved.
- `config.yaml` is committed to your repo, but it holds no secrets — only
  your triage preferences and delivery addresses.

---

## Running it locally

```bash
git clone https://github.com/YOUR-USERNAME/mailtriage
cd mailtriage
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

export ANTHROPIC_API_KEY=sk-ant-...
export RESEND_API_KEY=re_...
export MAIL_ACCOUNTS=alice@gmail.com
export MAIL_PW_ALICE_GMAIL_COM=...

.venv/bin/mailtriage --self-check   # assertions only, no API calls, no network
.venv/bin/mailtriage --dry-run      # real IMAP pull + real API call, prints instead of sending
.venv/bin/mailtriage                # real run, actually delivers
```

```bash
.venv/bin/pytest                                             # full test suite
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

---

## What's in here

```
src/mailtriage/
  errors.py        MailError — the only exception raised on purpose
  models.py         Email (pulled message), Triaged (a bucketed, annotated one)
  config.py         config.yaml -> validated Config dataclass
  imap_pull.py       account/password lookup, IMAP fetch, time-window filter
  triage.py          the triage prompt + the forced-tool Claude call   <- the product
  delivery/          __init__ dispatch, http.py, mail.py (Resend + the email template)
  selfcheck.py       the pre-flight assertions
  cli.py             argparse; the only module that prints and exits
tests/               pytest suite
config.yaml          your triage settings (committed, holds no secrets)
docs/SETUP.md        one-time credential setup (2-Step Verification + app passwords)
.github/workflows/digest.yml   the schedule
.github/workflows/ci.yml       lint + types + tests on every push
```

No build step, no framework, no `node_modules`, no server.

---

## License

MIT. Fork it, change it, ship it.
