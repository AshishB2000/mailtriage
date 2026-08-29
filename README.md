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

### The easy way: the setup wizard

1. Click **Fork** (top right of this page).
2. Open [`docs/index.html`](docs/index.html) from your fork — either locally
   (clone and open the file, or download it and double-click it, no server
   needed) or via GitHub Pages if you've enabled it on your fork.
3. Paste a [GitHub personal access token](https://github.com/settings/tokens/new?scopes=repo&description=mailtriage+setup),
   fill in your triage brief, AI auth, Resend key, and Gmail accounts, and
   click through.

The page finds your fork, encrypts every secret **in your browser** with
libsodium against your repository's own public key, writes them to your
fork's Actions secrets, commits `config.yaml`, and triggers the first run —
all directly against the GitHub API from that one tab. There's no server
behind it and nothing to install; open the page's source and read it if you
want to verify that yourself. Reopening the page later turns it into a
settings editor: pick your repo again and it loads your existing
`interests`, `avoid`, and delivery settings from `config.yaml` (secrets and
accounts still need re-entering — GitHub never returns a secret's value).

If you'd rather do it by hand — or the wizard hits something your setup
doesn't like — the manual steps below do exactly the same thing.

### The manual way

#### 1. Fork this repo

Click **Fork**. Everything below happens inside your fork.

#### 2. Enable Actions

Open the **Actions** tab of your fork. GitHub disables scheduled workflows
in forks by default, so you'll see a banner — click **"I understand my
workflows, go ahead and enable them."**

<!-- screenshot: Actions tab, enable-workflows banner -->

#### 3. Add the secrets

**Settings → Secrets and variables → Actions → New repository secret.**

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` **or** `CLAUDE_CODE_OAUTH_TOKEN` | AI auth — pick one, see below |
| `RESEND_API_KEY` | a key from [resend.com/api-keys](https://resend.com/api-keys) (free tier) — you'll also need to verify a sending domain at [resend.com/domains](https://resend.com/domains) |
| `MAIL_ACCOUNTS` | comma-separated Gmail addresses, e.g. `alice@gmail.com,alice.work@gmail.com` |
| one `MAIL_PW_*` per address | the app password for that address (see step 4) |

**AI auth — set exactly one of these two secrets:**

- **`ANTHROPIC_API_KEY`** (recommended for most forkers) — a key from
  [console.anthropic.com](https://console.anthropic.com/settings/keys).
  Pay-per-use, a few cents a month; works for anyone with an Anthropic
  account.
- **`CLAUDE_CODE_OAUTH_TOKEN`** (if you have a Claude Pro/Max subscription) —
  run `claude setup-token` locally once and paste the printed token as the
  secret value. Triage then runs against your subscription instead of the
  API, so there's no per-run API bill. The token lasts about a year;
  regenerate it with `claude setup-token` when it expires. Requires an
  active Claude subscription and the [Claude Code
  CLI](https://docs.claude.com/en/docs/claude-code) installed locally to run
  `setup-token` — the workflow installs the CLI on the runner automatically,
  only when this secret is set.

If neither secret is set, the run fails fast with `No Claude auth
configured`.

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

#### 4. Create an app password per account

For **each** Gmail address in `MAIL_ACCOUNTS`:

1. Enable 2-Step Verification at [myaccount.google.com/security](https://myaccount.google.com/security).
2. Create an app password (choose "Mail") at
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Paste the 16-character value into the matching `MAIL_PW_*` secret above.

Full walkthrough: [docs/SETUP.md](docs/SETUP.md).

#### 5. Edit `config.yaml` and commit

Open `config.yaml` in GitHub's web editor (or clone and edit locally) and set
at least `email_to` and `email_from`. `interests`, `avoid`, and
`window_hours` ship with sane defaults but are worth tuning to your own
inbox. Commit the change — `config.yaml` holds no secrets and is meant to be
committed.

#### 6. Trigger a first run

**Actions → digest → Run workflow**, or just wait for the next scheduled
run. Check the log if nothing arrives — see Troubleshooting below.

---

## Delivery options

`delivery` in `config.yaml` picks one of two backends:

- **`email`** (default) — sends via [Resend](https://resend.com). Needs the
  `RESEND_API_KEY` secret and `email_from` on a domain you've verified at
  [resend.com/domains](https://resend.com/domains) (or Resend's shared
  `onboarding@resend.dev` sender, which can only deliver to your own
  Resend-account address). Can deliver to any address.
- **`gmail`** — sends through your own Gmail via SMTP, authenticating with
  the same app password you already set up for `imap_pull` to read that
  inbox. No Resend account, no domain verification. `email_from` must be one
  of your `MAIL_ACCOUNTS` Gmail addresses with its `MAIL_PW_*` secret set;
  mail from your Gmail to your Gmail lands straight in the inbox. Best when
  `email_to` is the same address (or another of your own accounts).

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
| `No Claude auth configured` | Neither `ANTHROPIC_API_KEY` nor `CLAUDE_CODE_OAUTH_TOKEN` is set | Set one of the two — see step 3 above |
| `ANTHROPIC_API_KEY is not set.` | The secret is missing | Add it under Settings → Secrets and variables → Actions |
| `Anthropic rejected ANTHROPIC_API_KEY.` | The secret exists but the key is wrong, revoked, or has a stray space | Generate a fresh key and update the secret |
| `` `claude` CLI exited with status ... `` (subscription mode) | Usually an expired or invalid `CLAUDE_CODE_OAUTH_TOKEN` | Regenerate the token locally with `claude setup-token` and update the secret |
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
  delivery/          __init__ dispatch, http.py, mail.py (Resend), gmail.py (your own Gmail via SMTP)
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
