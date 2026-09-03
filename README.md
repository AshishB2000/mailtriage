<h1 align="center">mailtriage</h1>

<p align="center"><b>AI triages every Gmail account you have, on your own schedule, and drafts the replies for you — you open your inbox to find the answers already written.</b></p>

<p align="center">Fork-and-run · your own GitHub Actions · your own AI credentials · no server, no accounts, nothing routes through anyone else.</p>

<p align="center">
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-blue.svg?style=flat" /></a>
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat" />
  <img alt="providers" src="https://img.shields.io/badge/AI%20providers-6-informational?style=flat" />
  <img alt="cost" src="https://img.shields.io/badge/cost-%240%20possible-success?style=flat" />
</p>

---

mailtriage reads the INBOX of one or more Gmail accounts, sorts what actually
arrived into **needs action** / **worth reading** — and drops the rest —
then sends you a single short HTML email. For everything that needs a reply,
it also drafts one: into the digest, and appended straight into that
account's Gmail Drafts folder, threaded to the original message. Everything
else is noise it never shows you: newsletters, receipts, promotions,
automated notifications.

It runs on **your** GitHub Actions, with **your** AI provider credentials —
Claude, ChatGPT, OpenAI, or Gemini, whichever you already pay for (or a free
Google account), reading
**your** Gmail over read-only IMAP. Your laptop can be off. There's no
server, no database, no accounts, and nothing routes through anyone else —
you fork this repo and it becomes entirely yours.

---

## Bring the AI you already pay for

mailtriage never bills you directly — it runs entirely on your own
credentials, in your own GitHub Actions. `provider` in `config.yaml` (default
`"auto"`) picks the first secret below that's set; set it explicitly to force
one instead.

**Subscription CLIs — pay nothing extra, on top of a plan you already have:**

| `provider` | Secret | Notes |
|---|---|---|
| `claude-subscription` | `CLAUDE_CODE_OAUTH_TOKEN` | Requires a Claude Pro/Max subscription. Run `claude setup-token` locally once and paste the printed token as the secret value. Lasts about a year; regenerate the same way when it expires. |
| `chatgpt-subscription` | `CODEX_AUTH_JSON` | Requires a ChatGPT Plus/Pro subscription. Run `codex login` locally, then paste the **full contents** of `~/.codex/auth.json`. **Honest caveat:** Codex rotates its tokens during use, and a stateless CI runner can't persist that rotation back to the secret — expect to re-run `codex login` and re-paste occasionally when a run fails with an auth error. |
| `google-subscription` | `GEMINI_OAUTH_JSON` | **Free** — no subscription needed, just a personal Google account: 60 requests/min, 1,000/day. Run `gemini` locally, sign in with Google, then paste the **full contents** of `~/.gemini/oauth_creds.json`. **Honest caveat:** Google's refresh token dies if unused for 6 months, or if you revoke access — re-run `gemini` locally, sign in again, and re-paste when a run fails with an auth error. |

**API keys — pay-per-use, billed by the provider directly:**

| `provider` | Secret | Notes |
|---|---|---|
| `claude-api` | `ANTHROPIC_API_KEY` | [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys). A few cents a month for this workload. |
| `openai-api` | `OPENAI_API_KEY` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys). |
| `gemini-api` | `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — Gemini's free tier (roughly 10 requests/minute) comfortably covers two runs a day, so this option can run mailtriage at **$0**. |

Set exactly one of the six secrets above. If none is set, the run fails fast
with `No AI provider configured`, listing all six options and their secret
names — see [Troubleshooting](#troubleshooting).

---

## Reply drafting

Every `needs_action` item gets a model-drafted reply, in plain text, ready to
send after a quick human read. You'll find it two places:

- **In the digest email**, right under that item's note.
- **In Gmail Drafts**, on the account the message arrived at, threaded to the
  original via `In-Reply-To`/`References` — open the thread in Gmail and the
  draft is already sitting there.

**mailtriage never sends anything, on its own or otherwise:**

- `imap_pull.py` has no SMTP call anywhere in it, by design.
- The INBOX connection stays `select("INBOX", readonly=True)` for the entire
  run — reply drafting reads nothing new from INBOX, it only writes to
  Drafts.
- Pushing a draft only ever **appends** a new message to the account's
  `\Drafts` mailbox (`imaplib`'s `APPEND`) — it never touches an existing
  message, never sets a flag on one, and never selects INBOX for write.
- The model is instructed to leave a bracketed `[placeholder]` for any
  fact it doesn't actually know, rather than invent one.

Set `draft_replies: false` in `config.yaml` to turn this off — the digest
still triages normally, it just stops drafting.

---

## What it looks like

```
mailtriage · 2 to act · 3 to read

NEEDS ACTION

  Q3 budget sign-off — due Friday
  ● Priya Shah · alice.work@gmail.com
  She needs your approval on the revised number by EOD Friday to hit
  the finance deadline.

  Draft reply
  Hi Priya, the revised number looks good to me — approved. Let me
  know if you need anything else before Friday.
  Thanks,

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
   fill in your triage brief, pick one AI provider from the picker and paste
   its credential, add your Gmail accounts, pick your schedule and timezone,
   set any VIP rules or draft style you want, and click through. For
   delivery, pick **Gmail** (recommended — sends from your own Gmail to
   itself, nothing else to set up) or **Resend** (paste an API key and a
   verified from-domain to send from a custom domain).

The page finds your fork, encrypts every secret **in your browser** with
libsodium against your repository's own public key, writes them to your
fork's Actions secrets, commits `config.yaml` (with your chosen `provider`
written explicitly, not `"auto"`, and `window_hours` computed from the
`run_at` times you picked), and triggers the first run — all directly
against the GitHub API from that one tab. There's no server behind it and
nothing to install; open the page's source and read it if you want to verify
that yourself. Reopening the page later turns it into a settings editor: pick
your repo again and it loads your existing `interests`, `avoid`, `provider`,
schedule, rules, and draft-style settings from `config.yaml` (secrets and
accounts still need re-entering — GitHub never returns a secret's value; a
per-account override reappears once you retype that account's address).

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
| one AI-auth secret | pick one from the [provider matrix above](#bring-the-ai-you-already-pay-for) |
| `RESEND_API_KEY` | a key from [resend.com/api-keys](https://resend.com/api-keys) (free tier) — you'll also need to verify a sending domain at [resend.com/domains](https://resend.com/domains) |
| `MAIL_ACCOUNTS` | comma-separated Gmail addresses, e.g. `alice@gmail.com,alice.work@gmail.com` |
| one `MAIL_PW_*` per address | the app password for that address (see step 4) |
| `EMAIL_TO` *(optional)* | where the digest is delivered. For `delivery: gmail`, defaults to your first `MAIL_ACCOUNTS` address if unset |
| `EMAIL_FROM` *(optional)* | who it's sent from. Same default as above for `delivery: gmail`; required for `delivery: email` (Resend) |

**The `MAIL_PW_*` names are the most error-prone step.** Each is
`MAIL_PW_` + the first 16 hex characters (upper-cased) of a BLAKE2b-128 hash
of the address, trimmed and lower-cased — a hash rather than the address
itself, because secret *names* appear in your fork's public Actions log and
your address shouldn't. It's exactly what `imap_pull.pw_env_var` computes at
run time; get the name for any address with one line of stdlib Python:

```bash
python3 -c 'import hashlib,sys; print("MAIL_PW_" + hashlib.blake2b(sys.argv[1].strip().lower().encode(), digest_size=16).hexdigest()[:16].upper())' alice@gmail.com
# MAIL_PW_F24FE3C393F64986
```

Get the name wrong and that one account is skipped with a warning in the
Actions log — the run still completes for every other account.

> **Upgrading an older fork?** The previous names — `MAIL_PW_` + the address
> upper-cased with every non-alphanumeric turned into `_`, e.g.
> `MAIL_PW_ALICE_GMAIL_COM` — are deprecated but still read as a fallback, so
> existing secrets keep working. Re-running the setup wizard writes the hashed
> name; delete the old secret afterwards if you'd rather it not name your
> address.

#### 4. Create an app password per account

For **each** Gmail address in `MAIL_ACCOUNTS`:

1. Enable 2-Step Verification at [myaccount.google.com/security](https://myaccount.google.com/security).
2. Create an app password (choose "Mail") at
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Paste the 16-character value into the matching `MAIL_PW_*` secret above.

Full walkthrough: [docs/SETUP.md](docs/SETUP.md).

#### 5. Edit `config.yaml` and commit

Open `config.yaml` in GitHub's web editor (or clone and edit locally). Leave
`email_to` and `email_from` as `""` — those addresses go in the `EMAIL_TO` /
`EMAIL_FROM` secrets from step 3 instead, since this file is committed and a
fork can be public. `interests`, `avoid`, `window_hours`, `provider`, and
`draft_replies` ship with sane defaults but are worth tuning to your own
inbox. Commit the change — `config.yaml` holds no secrets and is meant to be
committed.

#### 6. Trigger a first run

**Actions → digest → Run workflow**, or just wait for the next scheduled
run. Check the log if nothing arrives — see Troubleshooting below.

---

## Delivery options

`delivery` in `config.yaml` picks one of two backends:

- **`email`** (default) — sends via [Resend](https://resend.com). Needs the
  `RESEND_API_KEY` secret and `EMAIL_FROM` on a domain you've verified at
  [resend.com/domains](https://resend.com/domains) (or Resend's shared
  `onboarding@resend.dev` sender, which can only deliver to your own
  Resend-account address). Can deliver to any address. No fallback — a
  verified sender can't be guessed, so `EMAIL_TO` and `EMAIL_FROM` are both
  required.
- **`gmail`** — sends through your own Gmail via SMTP, authenticating with
  the same app password you already set up for `imap_pull` to read that
  inbox. No Resend account, no domain verification. `EMAIL_FROM` must be one
  of your `MAIL_ACCOUNTS` Gmail addresses with its `MAIL_PW_*` secret set;
  mail from your Gmail to your Gmail lands straight in the inbox. Leave
  `EMAIL_TO` / `EMAIL_FROM` unset entirely to self-mail: both default to the
  first `MAIL_ACCOUNTS` address.

---

## What you'll need, and what it costs

| | Where | Cost |
|---|---|---|
| **One AI-auth credential** | see the [provider matrix above](#bring-the-ai-you-already-pay-for) | free with a subscription CLI, a few cents/month for an API key, or $0 on Gemini's free tier |
| **Resend key** | [resend.com](https://resend.com) | Free tier covers personal volume |
| **GitHub Actions** | already have it | Free minutes are ample for 2 runs/day |

Each run is one AI call with your recent INBOX mail in and a short triage
out, plus one more call to draft replies when there's anything to reply to —
this is headline-scale triage, not a long conversation. Two runs a day
lands in the low single-digit cents per month for a typical inbox on an API
key, or $0 on a subscription CLI or Gemini's free tier. You're billed
directly by your chosen AI provider and by Resend; nothing goes through this
project.

---

## How it works

1. **Gate** — `.github/workflows/digest.yml` runs *hourly*; `mailtriage --due`
   checks whether the current hour matches one of your `run_at` times (or
   your `weekly_review` slot) in your `timezone`, and skips the rest of the
   job if not. See [Your schedule](#your-schedule) below.
2. **Pull** — connect to each Gmail account over IMAP, **read-only**
   (`readonly=True`, `BODY.PEEK[]`) — mailtriage never marks anything read.
3. **Window** — keep only messages inside `window_hours`; drop anything
   undated.
4. **Rules** — `rules.always_ignore` drops matching senders before the model
   ever sees them. See [Your rules](#your-rules).
5. **Triage** — one call to whichever AI provider `provider` in `config.yaml`
   selects (see the provider matrix above), with your `interests` and `avoid`
   text (plus any per-account overrides, see
   [Per-account settings](#per-account-settings)) and the windowed messages,
   constrained to a strict schema. The model returns bucket + one-line note
   per message it's keeping, referenced by an integer index — never a URL, so
   there's no risk of the model inventing or mangling a link.
   `rules.always_surface` / `rules.always_action` are then applied on top of
   what the model returned, deterministically — a rule always wins.
6. **Draft** — if `draft_replies` is on (default) and anything landed in
   `needs_action`, a second call drafts a reply for each, in the style set by
   `draft_style` (see [Draft style](#draft-style)). Drafts are appended to
   the source account's Gmail Drafts mailbox — never sent — and shown in the
   digest.
7. **Send** — one HTML email via Resend, or nothing if both buckets came
   back empty.

**No state, anywhere** — there's no database and no record of what was
already sent. `window_hours` is the dedupe: it must be at least as long as
the largest gap between two consecutive `run_at` slots, or mail that arrives
inside the gap is silently skipped forever and never appears in any digest.
The setup wizard computes `window_hours` for you from `run_at` every time it
writes `config.yaml`; if you hand-edit `run_at`, update `window_hours` to
match (`mailtriage --self-check` doesn't catch a stale value — only a real
run's stderr warning does).

---

## Your schedule

Pick your own digest times instead of a fixed cadence. In `config.yaml`:

```yaml
run_at: ["08:00", "18:00"]     # as many as you like, "HH:MM" 24h
timezone: "America/New_York"   # IANA name — any tz database zone
weekly_review: ""              # "" = off, or "<mon..sun> HH:MM" for a weekly slot
catch_up_minutes: 120          # how late the hourly gate still fires a slot (60-360)
```

`.github/workflows/digest.yml` itself never changes — it can't, without every
fork granting the setup wizard write access to workflow files it doesn't
need for anything else. Instead it runs on a fixed hourly cron (`17 * * * *`
— GitHub's own docs warn the top of the hour gets crowded, so this dodges
it), and `mailtriage --due` decides whether *this* hour is one of your
`run_at` slots before anything else runs. Two things follow from that:

- **Runs fire within about an hour after the time you picked**, not on the
  minute — GitHub's scheduler can drift 5-30 minutes late under load, and
  sometimes skips an hour outright (we've seen one run in five hours). The
  gate accepts a slot for `catch_up_minutes` after it (default 120, valid
  60-360), so the hour after a skipped one still sends that slot's digest.
- **A slot never sends twice.** Two hourly firings can both land inside one
  slot's catch-up window, so each scheduled digest's subject carries its
  slot — `mailtriage · Thu 03 Sep 08:00 · 2 to act · 3 to read` — and
  before sending, the engine looks for that subject in the mailboxes it
  already reads (All Mail, since yesterday). Found → it sends nothing and
  says so in the log. Gmail is the memory; there's still no state file.
  Manual "Run workflow" clicks and `--dry-run` carry no slot and are never
  suppressed. (With `delivery: email` to an address outside `MAIL_ACCOUNTS`
  the guard can't see the sent digest and quietly does nothing.)
- **A slot missed for longer than `catch_up_minutes` doesn't lose mail** —
  `window_hours` overlaps between runs specifically so a dropped trigger's
  mail shows up at the next slot instead of vanishing.

The setup wizard's Schedule step picks your timezone (defaulting to the one
your browser reports), lets you add/remove `run_at` times, and computes
`window_hours` for you — see [How it works](#how-it-works) above for why that
number matters.

---

## Your rules

Hard VIP-sender rules, checked deterministically — no API call, so they
never depend on the model getting it right:

```yaml
rules:
  always_ignore: ["newsletter@example.com"]   # dropped before the model ever sees them
  always_surface: ["@vip-client.com"]         # always worth_reading, bypasses reading_count
  always_action: ["boss@corp.com"]            # always needs_action, wins over always_ignore
```

Each entry is a full address or a domain rule starting with `@` (which also
matches its subdomains). If an address appears in both `always_ignore` and
`always_action`, action wins — ignoring is your general "don't bother me"
setting, and a more specific `always_action` entry for the same sender means
you decided their messages must never be silenced.

---

## Draft style

Tone, sign-off, language, and length for AI-drafted replies:

```yaml
draft_style:
  tone: friendly       # friendly | formal | casual
  sign_off: ""          # e.g. "Best, Alex" -- overrides the generic "Thanks,"
  language: auto        # or a language name, e.g. "French"
  max_sentences: 5
```

`language: auto` matches the sender's language rather than always replying
in one. Set `draft_replies: false` to turn drafting off entirely — see
[Reply drafting](#reply-drafting) above.

---

## Per-account settings

If you triage more than one Gmail account, each can have its own interests,
avoid list, and draft style — added to (interests/avoid) or merged over
(draft_style) the global settings above:

```yaml
accounts:
  work@corp.com:
    interests: |
      Anything from the eng-leads mailing list counts as needing action.
    draft_style:
      tone: formal
```

Leave a key out of a per-account entry and it inherits the global value —
only what you actually set here overrides it.

---

## Never lose an action item

`carry_over: true` (default) applies a Gmail label — `label:
"mailtriage/action"` by default — to every `needs_action` message, and keeps
re-listing it in every digest until you reply, archive, or remove the label
yourself. It's Gmail's own labels doing the remembering, not a database this
project runs: nothing is stored outside your inbox, and there's still no
`seen.json`, no repo state, and no per-run commit. Turn it off with
`carry_over: false` if you'd rather each digest be a clean snapshot of the
current `window_hours` only.

---

## The 60-day caveat

GitHub automatically disables scheduled workflows on a repo after **60 days
with no activity** at all — not specific to this project, it applies to
every scheduled workflow on every repo. If your digest stops arriving,
that's almost always why. Fix: **Actions** tab → re-enable the workflow, or
just push any commit to reset the clock.

---

## Keeping your fork current

`.github/workflows/upstream-sync.yml` runs on the first of every month — and
on demand from **Actions → upstream-sync → Run workflow** — in forks only.
If this project's `main` has moved on since your last sync, it opens a pull
request against your fork from a `mailtriage/upstream-sync` branch; review
it, merge it, done. Your `config.yaml` is never clobbered: wherever it
conflicts with upstream, the merge keeps your fork's version. If anything
*else* conflicts (you edited the engine or a workflow yourself), the job
fails with a message pointing you at GitHub's **Sync fork** button on your
fork's front page, where you can resolve it by hand.

One setting to flip once, or the workflow can't open the PR: **Settings →
Actions → General → Workflow permissions → tick "Allow GitHub Actions to
create and approve pull requests"**.

Merging a sync PR is a commit, so it also resets the
[60-day clock](#the-60-day-caveat) above — a fork that takes the monthly
sync never goes quiet long enough for GitHub to switch the schedule off.

The actions this repo uses are pinned to full commit SHAs, and
`.github/dependabot.yml` keeps those pins fresh upstream (Dependabot is off in
forks by default; its bumps reach you through the sync PR instead).

---

## Troubleshooting

**Start with the doctor.** Actions → digest → **Run workflow** → set
`mode` to `doctor` (or run `mailtriage --doctor` locally). It prints one
`PASS`/`FAIL` line per check, with the fix in the `FAIL` line, and exits 1
if anything failed:

```
doctor: PASS config — config.yaml loads
doctor: PASS account alice@gmail.com — ok: 1432 in INBOX
doctor: PASS provider claude-subscription — the fixture's contract request came back as needs_action
doctor: PASS delivery gmail — test message sent — check the inbox it should land in
```

The provider check triages a fixed three-email fixture (one obvious
action item, a newsletter, a receipt), so it costs one small model call;
the delivery check really sends one line to your digest address — that's
the point. Every scheduled run also prints
`mailtriage: usage input=N output=N cost=$X.XXXX` after each model call
(cost only where the backend reports it — the `claude` CLI does), so you
can see what a run costs without opening a billing page.

Otherwise: Actions tab → open the failed run → read the log. mailtriage's
errors are written to say what's wrong and how to fix it, not just that
something failed.

| Log message | What it means | Fix |
|---|---|---|
| `No AI provider configured` | None of the six AI-auth secrets is set | Set one — see the provider matrix above |
| `ANTHROPIC_API_KEY is not set.` | The secret is missing | Add it under Settings → Secrets and variables → Actions |
| `Anthropic rejected ANTHROPIC_API_KEY.` | The secret exists but the key is wrong, revoked, or has a stray space | Generate a fresh key and update the secret |
| `` `claude` CLI exited with status ... `` (subscription mode) | Usually an expired or invalid `CLAUDE_CODE_OAUTH_TOKEN` | Regenerate the token locally with `claude setup-token` and update the secret |
| `Anthropic rate-limited this run, or the account is out of credit.` | Billing/rate limit on the Anthropic side, not a bug | Check your balance; the next scheduled run picks things up |
| `OpenAI rejected OPENAI_API_KEY.` | The secret exists but the key is wrong, revoked, or has a stray space | Generate a fresh key at platform.openai.com/api-keys and update the secret |
| `Google rejected GEMINI_API_KEY.` | The secret exists but the key is wrong, revoked, or has a stray space | Generate a fresh key at aistudio.google.com/apikey and update the secret |
| `No Codex auth configured` / `codex CLI ... tokens in CODEX_AUTH_JSON have likely expired or rotated` | `CODEX_AUTH_JSON` is missing, or its tokens rotated since it was pasted in | Run `codex login` locally and re-paste the new `~/.codex/auth.json` into the secret, or switch to `OPENAI_API_KEY` |
| `` `gemini` CLI exited with status ... `` (authentication error) | `GEMINI_OAUTH_JSON` is missing, or the refresh token expired (unused 6 months) or was revoked | Run `gemini` locally, sign in again, and re-paste the new `~/.gemini/oauth_creds.json` into the secret |
| `MAIL_ACCOUNTS is empty` | The secret isn't set, or is blank | Set it to a comma-separated list of Gmail addresses |
| `<addr>: no app password found in $MAIL_PW_...` | That account's `MAIL_PW_*` secret is missing or misnamed | Re-check the name against the transform in step 3 above; this account is skipped, the run continues for the rest |
| `RESEND_API_KEY is not set.` | The secret is missing | Add it from resend.com/api-keys |
| `email_to is empty in config.yaml.` | No `EMAIL_TO` secret and none set in `config.yaml` (Resend delivery has no fallback) | Add the `EMAIL_TO` secret |
| `email_from is empty...` | Same, for the sender address (gmail delivery falls back to your first `MAIL_ACCOUNTS` address instead of failing) | Add the `EMAIL_FROM` secret — for Resend it must be on a domain verified with Resend |
| `Resend refused the email (HTTP 403)...` | Almost always an unverified sending domain, **not** a bad API key | Verify the domain at resend.com/domains |
| `could not reach api.resend.com` / `could not reach api.anthropic.com` | The runner had no network, or the API was briefly down | Re-run the workflow by hand |
| `the model's reply was cut off (stop_reason=max_tokens)` | Too much input for the reply budget | Lower `reading_count` in `config.yaml`, or shorten `interests` |
| `mailtriage: account failed, skipping: ...` (per-account warning) | One account's IMAP login failed (bad password, network) | That account is skipped; every other account still triages normally |
| `mailtriage: draft push failed, skipping: ...` (per-account warning) | Drafting worked but appending to that account's Drafts mailbox failed | Digest still sends with the drafts inline; that account's Gmail Drafts just didn't get them this run |
| `mailtriage: nothing recent — sending nothing.` | No mail in the window at all | Nothing to fix — normal on a quiet window |
| `mailtriage: this slot's digest was already delivered — sending nothing.` | An earlier hourly run inside this slot's `catch_up_minutes` window already sent it; the engine found the slot-stamped subject in your mailbox | Nothing to fix — this is the no-double-send guard doing its job. See [Your schedule](#your-schedule) |
| `doctor: FAIL ...` | `mailtriage --doctor` found a broken piece of the setup | The rest of the line says what to change; the table below covers the same messages |
| `mailtriage: the model kept none of the candidates — sending nothing.` | The model triaged everything as noise | Working as intended, not a failure |

---

## Privacy

- Your mail never leaves your infrastructure except to the two services you
  configured yourself: your chosen AI provider (to triage and draft) and
  Resend (to deliver, unless you chose `delivery: gmail`). Nothing routes
  through the maintainer of this project — no analytics, no telemetry, no
  hosted anything.
- IMAP access to INBOX is **read-only**: mailtriage selects `INBOX` with
  `readonly=True` and fetches with `BODY.PEEK[]`, so it can never mark a
  message as read or change anything in your inbox. Drafting only ever
  **appends** to the Drafts mailbox — it never sends and never touches an
  existing message.
- Secrets (API keys, app passwords, and your `EMAIL_TO`/`EMAIL_FROM`
  addresses) live only in your fork's GitHub Actions secrets — encrypted at
  rest by GitHub, not readable back by anyone including you, once saved.
- `config.yaml` is committed to your repo — and this repo may be public, if
  your fork is — so it holds only your triage preferences, never an address
  or a secret. `email_to`/`email_from` ship blank on purpose.

---

## Running it locally

```bash
git clone https://github.com/YOUR-USERNAME/mailtriage
cd mailtriage
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

export ANTHROPIC_API_KEY=sk-ant-...   # or any other secret from the provider matrix above
export RESEND_API_KEY=re_...
export MAIL_ACCOUNTS=alice@gmail.com
export MAIL_PW_F24FE3C393F64986=...   # pw_env_var("alice@gmail.com") -- see manual setup, step 3

.venv/bin/mailtriage --self-check   # assertions only, no API calls, no network
.venv/bin/mailtriage --doctor       # config + IMAP login + provider + delivery, PASS/FAIL per check
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
  schedule.py        "is it time?" -- max_gap_hours, due() -- pure, no I/O
  rules.py           VIP-sender rules, checked deterministically around the model
  imap_pull.py       account/password lookup, IMAP fetch, time-window filter, push_drafts
  triage/            the triage prompt (__init__.py)   <- the product
                       + 6 backends: claude_api, claude_cli, codex_cli, openai_api, gemini_api, gemini_cli
  drafts.py          the reply-drafting prompt + hostile-input-safe id mapping
  delivery/          __init__ dispatch, http.py, mail.py (Resend), gmail.py (your own Gmail via SMTP)
  selfcheck.py       the pre-flight assertions
  cli.py             argparse; the only module that prints and exits
tests/               pytest suite
config.yaml          your triage settings (committed, holds no secrets)
docs/index.html      the zero-backend setup wizard (+ vendored sodium.js)
docs/SETUP.md        one-time credential setup (2-Step Verification + app passwords)
CHANGELOG.md          what changed, by release
.github/workflows/digest.yml   the schedule
.github/workflows/ci.yml       lint + types + tests on every push
.github/workflows/upstream-sync.yml   monthly "pull in upstream" PR, forks only
.github/dependabot.yml         keeps the SHA-pinned actions above current
```

No build step, no framework, no `node_modules`, no server.

---

## License

MIT. Fork it, change it, ship it.
