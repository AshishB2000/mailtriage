# mailtriage — working notes for Claude

Fork-and-run AI triage of Gmail inboxes. Users **fork** this repo and run it on
their own GitHub Actions with their own credentials. No server, no database, no
accounts. The maintainer never pays for anyone else's inference.

## Commands

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/python -m pytest -q
                                    # THE GATE — all four, before every push
.venv/bin/mailtriage --self-check   # assertions only, no network, no API, no creds
.venv/bin/mailtriage --doctor       # PASS/FAIL per check: config, IMAP login, provider (one small call), delivery (one real send)
.venv/bin/mailtriage --dry-run      # real fetch + triage, prints instead of sends
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

gh workflow run digest.yml --ref main          # trigger a live run
gh run view <id> --log-failed                  # why it failed
gh release create vX.Y.Z --target main ...     # release = tag + notes; bump pyproject + __init__ first
```

## Layout

```
src/mailtriage/
  errors.py     MailError — the ONLY exception raised on purpose
  models.py     Email (triage input), Triaged (output), PullResult
  imap_pull.py  read-only IMAP fetch; pw_env_var is the secret-name transform;
                push_drafts APPENDs to \Drafts only
  config.py     config.yaml -> validated Config dataclass
  schedule.py   "is it time?" -- max_gap_hours, due(), current_slot() -- pure, no I/O
  rules.py      always_ignore/always_surface/always_action, checked around
                the model, never inside the prompt
  triage/       package, dict-dispatch pattern   <- the product
                  __init__.py: prompt + schema + pick() + PROVIDERS
                  claude_api.py claude_cli.py codex_cli.py openai_api.py gemini_api.py gemini_cli.py
  drafts.py     reply-drafting prompt + hostile-input-safe id mapping
  commands.py   Gmail as the control plane: done/snooze/never/vip labels,
                replies to the digest ("done 3"), never/vip sender derivation
  delivery/     dispatch, http.py, text.py (the one plain-text renderer),
                mail.py (Resend), gmail.py (own-Gmail SMTP),
                telegram.py slack.py discord.py ntfy.py (chat/push channels)
  selfcheck.py  pre-flight assertions, run before any API spend
  cli.py        argparse; the only module that prints and exits
docs/index.html the zero-backend setup wizard (+ vendored sodium.js)
```

**Library code raises `MailError`, it never exits.** Only `cli.main()` catches,
prints to stderr, returns 1. Messages are written for someone staring at a red
workflow log — say what to change and where.

## The contracts — break these and forks silently break

**config.yaml** is written by the wizard, read by the engine, shipped as the
committed default. The field names on `config.Config` ARE the contract; unknown
keys warn, they never fail. `tests/test_config.py` loads the committed file.
The wizard writes the **full** config every time it fires — no partial
writes, no merge with what was there — so a field the wizard doesn't yet
have UI for should still get an explicit default in `buildYaml()`, never be
left out of the file.

```
delivery: "email" | "gmail" | "telegram" | "slack" | "discord" | "ntfy"
telegram_chat_id: str              digest_format: "html" | "text"
profiles: {name: {accounts: [addr, …], <any key here as an override>}}
interests: str (multiline)
avoid: str (multiline)             reading_count: int
window_hours: int                  run_at: list[str] ("HH:MM", wizard
                                      auto-derives this from run_at — see
                                      schedule.max_gap_hours)
timezone: str (IANA)               weekly_review: str ("" or "<day> HH:MM")
catch_up_minutes: int (60..360)    (how late the hourly gate still fires a slot)
subject_prefix: str                email_to: str        email_from: str
provider: str                      model: str
draft_replies: bool                draft_style: {tone, sign_off, language,
draft_variants: 1 | 2                 max_sentences, learn_voice}
rules: {always_ignore, always_surface, always_action}   accounts: {addr: {…}}
carry_over: bool                   label: str
nag_after_days: int
thread_context: bool               sender_memory: bool
show_unsubscribe: bool             noise: {label: bool, archive: bool}
                                      (archive requires label; both off)
```

**Label names** are fixed literals in `commands.py`, quoted in the digest
footer (`delivery/mail.py` COMMANDS_HINT) and the README -- change all three
together:

```
mailtriage/done   mailtriage/snooze-<N>d (1..90) | -1w | -2w   mailtriage/until-YYYY-MM-DD
mailtriage/never  mailtriage/vip                                mailtriage/handled
```

**Secret names** — used by the wizard (writes), the workflow (exports all via
`toJSON(secrets)`), and the engine (reads env):

```
one of: CLAUDE_CODE_OAUTH_TOKEN  ANTHROPIC_API_KEY  CODEX_AUTH_JSON
        OPENAI_API_KEY           GEMINI_API_KEY      GEMINI_OAUTH_JSON
                                                       (exactly one AI secret)
RESEND_API_KEY      MAIL_ACCOUNTS      MAIL_PW_<HASH> (one per address)
one of, by delivery: TELEGRAM_BOT_TOKEN  SLACK_WEBHOOK_URL  DISCORD_WEBHOOK_URL
                     NTFY_TOPIC_URL   (the wizard PUTs exactly the chosen one)
```

**Delivery contract**: every module in `delivery/` exposes
`send(cfg, kept, stamp="")` and `send_html(cfg, subject, html)`, and is
registered in BOTH dicts in `delivery/__init__.py`
(`tests/test_contracts.py` pins `BACKENDS == BACKENDS_HTML == DELIVERIES`).
The chat channels render `text.render()` with their own bold/link
spellings -- escaping runs BEFORE wrapping, always -- and carry a prebuilt
HTML (the weekly review) as `text.html_to_text()`. Chunk limits: Telegram
3900 (hard cap 4096, `parse_mode: HTML`, never MarkdownV2), Slack 3000,
Discord 1900 (cap 2000, `flags: 4` suppresses embeds). `NTFY_TOPIC_URL` is
a secret because the topic name IS the credential.

**Profiles**: `Config.profile(name)` re-runs `from_mapping` over the base's
fields plus the overrides, so an override is validated by the same rules as
the top level (errors say `profiles.<name>`). With profiles, `cli.main`
loops `_run_profiles`: `--due` is due if ANY profile is, and on a scheduled
run each profile is re-checked and runs in its own due mode; a manual
dispatch or local run does all of them. `only=` on
`imap_pull.accounts_from_env` (forwarded by `pull`/`pull_open_actions`/
`pull_week`) is the account filter; an address outside MAIL_ACCOUNTS raises.
`window_hours` stays global -- the wizard derives it from the top-level
`run_at` only (documented in README).

The wizard's provider picker writes an explicit `provider:` (never `"auto"`)
and PUTs exactly the one secret for that provider — the other four are never
written. `"auto"` (config.yaml default for hand-edited files) walks
`triage.PROVIDERS` in order and takes the first secret that's set.

`MAIL_PW_<HASH>` = `"MAIL_PW_" + blake2b(addr.strip().lower(),
digest_size=16).hexdigest()[:16].upper()` — a hash, never the address, because
secret *names* print in a public fork's Actions log. This transform exists in
TWO places that must match character-for-character: `imap_pull.pw_env_var`
(Python `hashlib`) and `mailPwSlug` (JS, docs/index.html, via the vendored
libsodium's `crypto_generichash(16, …)` — the same unkeyed BLAKE2b).
`tests/test_contracts.py` pins both sides to one vector,
`alice@gmail.com → MAIL_PW_F24FE3C393F64986`, and checks the wizard carries it
as a `// vector:` comment. The pre-hash `MAIL_PW_<SLUG>` (address upper-cased,
non-alphanumerics → `_`) is **deprecated**: `imap_pull.app_password` still
reads it as a fallback so old forks keep working; nothing writes it.

**The wizard reads as well as writes.** Its Dashboard screen
(docs/index.html, `loadDash`) is a read-only view of the fork through the
same REST API: `GET .../actions/workflows/digest.yml` (state, for the
60-day disable + `PUT .../enable`), `.../workflows/digest.yml/runs?per_page=10`
plus `.../runs/{id}/jobs` (a run whose "Send digest" step was skipped is
shown as *not a slot*), `.../actions/secrets` (names only -- it never sees a
value), `.../commits?per_page=1&sha=<default_branch>` (60-day timer),
`.../contents/config.yaml` (next slot, computed client-side from
`run_at`/`timezone`/`weekly_review` with Intl only -- the "next slot" half
of `schedule.due`, no catch-up), and
`GET /repos/AshishB2000/mailtriage/compare/main...<owner>:<default_branch>`
(`behind_by`). Its buttons `POST .../workflows/digest.yml/dispatches` with
`inputs: {mode}` -- `MODES` in the wizard is pinned to digest.yml's `mode`
options by `tests/test_contracts.py`; a 422 (fork's workflow predates the
input) retries input-less -- and `.../workflows/upstream-sync.yml/dispatches`.
`docs/sample-digest.html` is the preview it iframes: rendered by
`scripts/render_sample.py` through the real `email_html`, and
`tests/test_sample.py` fails if the committed file drifts from the template.

**The workflow must stay at `.github/workflows/digest.yml`** — the wizard
dispatches it by that literal filename.

**The bucket contract**: model returns integer `id` + `bucket` + one `note`,
nothing else. `needs_action` is UNCAPPED (hiding an action item is the worst
failure). `worth_reading` caps at `reading_count`, enforced client-side in
`pick()`. Noise is never returned — omission is the label. `pick()` copies
sender/subject/link from the real Email, never from the model.

Model: `claude-sonnet-5` (API path). This is headline triage on the user's
bill; don't upgrade to Opus without a reason. **The claude CLI argv is pinned to the one that has delivered a digest**
(`claude -p <system+user> --output-format json --json-schema …`, 1.0.0,
live 2026-09-01): no `--model` unless `model:` is set, no `--system-prompt`.
Each extra coincided with live runs returning 0 items from 30+ candidates
(`--model claude-sonnet-5`: 3 runs 2026-09-02; `--system-prompt` split:
2026-09-03, CLI 2.1.259). `tests/test_claude_cli.py` pins the argv. Codex and
gemini CLIs likewise pass no model unless `model:` is set — subscriptions
are flat-rate, the default costs nothing extra.

## Things that look wrong but are deliberate

- **No state, no seen-list.** `window_hours` (15 shipped) is the dedupe; it must stay ≥
  the largest cron gap (12h) or mail is skipped forever.
- **The no-double-send guard is a mailbox search, not a file.** `due()`
  accepts a slot for `catch_up_minutes` (120) because GitHub's cron skips
  hours; that lets two hourly firings share a slot, so every *scheduled*
  digest's subject carries `current_slot()` ("mailtriage · Thu 03 Sep
  08:00 · …") and `imap_pull.already_delivered` looks for it in \All
  before pull/triage. Manual runs (GITHUB_EVENT_NAME unset or
  workflow_dispatch) and `--dry-run` are unstamped and unguarded -- the
  Send step in digest.yml must keep passing GITHUB_EVENT_NAME for this to
  work at all. The guard is best-effort: a dead account never vetoes a send.
- **Empty digest sends nothing and exits 0.** "Nothing today" mails train users
  to unsubscribe.
- **A failed account warns and the run continues** — one bad login never blanks
  the digest. The broad `except Exception` per account is deliberate (imaplib
  raises 6+ unrelated types); `BLE001` is ignored repo-wide for this.
- **IMAP is read-only**: `select("INBOX", readonly=True)` + `BODY.PEEK[]`.
  Never let a change reintroduce plain `BODY[]` — it marks mail as read.
- **`triage/` never imports `anthropic` at module top.** `--self-check` must
  pass with the SDK uninstalled; the import lives inside `claude_api.call` only.
- **`delivery/mail.py` is named `mail`, never `email`** — shadows stdlib.
- **The wizard carries `profiles:` through verbatim** (`profilesBlock` in
  docs/index.html slices the raw text; `buildYaml` writes it back). It has
  no UI for profiles on purpose, and re-serializing a parse of the block
  would drop any YAML shape the subset parser doesn't know.
- **docs/sodium.js is vendored, not CDN** — the wizard must work from `file://`.
- **Provider auto-order is user-visible behavior, not an implementation
  detail.** `triage.PROVIDERS`' order — `claude-subscription` →
  `claude-api` → `chatgpt-subscription` → `openai-api` → `gemini-api` →
  `google-subscription` — is the precedence `"auto"` walks. Reordering it
  silently moves which secret (and which bill) an existing multi-secret fork
  lands on next run.
- **Gemini's `responseSchema` rejects `additionalProperties`.** `gemini_api.py`
  strips that keyword recursively before sending the schema. Don't "fix" the
  stripping away — Gemini 400s on the unmodified schema.
- **`CODEX_AUTH_JSON` tokens rotate**, and a stateless CI runner can't write
  the rotated value back to the secret. That's not a bug to fix — the 401
  error message already tells the user to re-run `codex login` and re-paste.
- **Digest items are numbered by `delivery.mail.digest_groups`, once.** The
  HTML, the `--dry-run` text and a reply's `#N` all come from that order.
  `_number()` renders the number as its own `<a href=gmail-link>#N</a>`, and
  `commands.item_map` reads `#N` straight off that anchor (and Gmail's
  `#N <url>` plain-text rendition) -- adjacency is what stops a "#3" the
  reader typed from pairing with the wrong link. Change both together.
- **`done`/snoozed messages are dropped from the run's candidates**
  (`apply_label_commands` returns their INBOX uids), not just un-labeled.
  An in-window item the reader just closed would otherwise be re-triaged
  and re-labeled as if nothing happened.
- **`until-*` label mailboxes are DELETEd once they've woken.** The only
  DELETE in the codebase; it removes an emptied label, never a message.
- **`Triaged.due` is optional (total=False base class)**, not a required
  key: Python 3.10 has no `typing.NotRequired`, and carried/rule-forced
  items never have one. Read it with `t.get("due", "")`.
- **Drafting never sends and never touches an existing message.**
  `push_drafts` only `APPEND`s to the account's `\Drafts` mailbox; INBOX
  stays `select(..., readonly=True)` for the whole run; there is no SMTP call
  anywhere in `imap_pull.py`. Don't add one.
- **`noise.archive` is the ONE opt-in exception to "never remove anything"**,
  and it only removes the `\Inbox` Gmail label (`-X-GM-LABELS (\Inbox)`)
  from mail `rules.omitted` returned -- never EXPUNGE, never DELETE, never a
  rule-protected sender, never on `--dry-run`, and it requires `noise.label`
  so the mail stays findable. Keep it that way.
- **Inbox intelligence is read-only and count-only.** `enrich` (thread
  context from `\All`, sender memory from `\Sent`) and `pull_voice_examples`
  (the reader's own Sent text, drafting prompt only) select `readonly=True`
  and fetch with `BODY.PEEK`; cli prints how many, never what. Voice
  examples must never reach stderr or the digest.

## Landmines (each one cost a real debugging session)

- **The hourly gate exits 3 for "not due", never 1.** `mailtriage --due` is
  the only thing standing between the hourly cron and a real triage run —
  `digest.yml` treats exit 0 as "run it" and exit 3 as "skip this hour", and
  any *other* nonzero exit as a real failure that must fail the job loudly.
  A "not due" path that returns 1 would make every off-hour run look like a
  broken workflow.
- **`--due` must never do I/O.** `schedule.due()` is pure — no network, no
  IMAP, no file writes beyond `load_config` itself — specifically so the
  hourly gate can run 24x a day without touching the mailbox or spending an
  API call on the 22ish hours it's not due.
- **Rules are deterministic and run around the model, not inside the
  prompt.** `rules.apply_ignore` runs before triage, `rules.enforce` runs
  after — see `cli.run()`. Folding VIP rules into the prompt instead would
  make them a suggestion the model can ignore; asking it to always flag
  `boss@corp.com` is exactly the kind of instruction a long inbox dump can
  bury.
- **`window_hours` is auto-derived in the wizard, not hand-typed.** The
  wizard computes it from `run_at` (`maxGapHours` in docs/index.html,
  mirroring `schedule.max_gap_hours` — pinned by
  `tests/test_contracts.py`) every time it writes the file. Hand-editing
  `run_at` in a committed `config.yaml` without also updating
  `window_hours` is still valid — `Config.from_mapping` only warns — but it
  reopens the missed-mail gap the wizard exists to close.
- **The wizard writes the full config every time, never a partial diff.**
  `buildYaml()` emits every `Config` field on every save; there's no
  "unchanged fields keep their old value" merge. Adding a new setting means
  adding it to `buildYaml()` with an explicit default, not assuming the
  shipped file already has it.
- **`secrets` context is illegal in a step-level `if:`.** GitHub rejects the
  whole workflow at dispatch time ("Unrecognized named-value: 'secrets'").
  Hoist to job-level `env:` and gate on `env.X`. Plain YAML parsing does NOT
  catch this.
- **`claude -p` reports errors on STDOUT, not stderr** — a JSON envelope with
  `is_error: true` and the reason in `result`, exit 1, stderr empty. Parse
  stdout first or every failure looks blank.
- **OAuth tokens picked up from a wrapped terminal line contain spaces.**
  A `sk-ant-oat01-…` token is ONE unbroken string; 401 "invalid" usually means
  a copy-paste space. Gmail app passwords are the opposite: displayed WITH
  spaces, stripped on use.
- **Resend cannot send from gmail.com** (403 "domain is not verified" — reads
  like a bad key, isn't). Options: verified domain, `onboarding@resend.dev`
  (spam-prone), or `delivery: gmail` which sends via the user's own Gmail SMTP
  reusing their existing MAIL_PW_* secret — no domain, lands in inbox.
- **Resend `to` must be a list**; a bare string 422s.
- **New GitHub accounts get an "Approve and run" gate** on Actions; scheduled
  runs sit waiting until the account's email is verified. Also: scheduled
  workflows auto-disable after 60 days without commits.
- **The wizard writes `provider:` explicitly, never `"auto"`.** The user made
  a choice in the picker; treat it as authoritative. It also PUTs exactly one
  of the five AI secrets — writing more than one lets a stale credential
  shadow the one the user meant to use.
- **`Email.date` is `datetime.isoformat()` output** (`+00:00` offset, never
  `Z`). Python 3.10's `fromisoformat` rejects `Z`; CI's 3.10 job is the floor,
  so test fixtures must use `+00:00` too — this bit us once.

## Style

Stdlib first — runtime deps are exactly `anthropic` + `PyYAML`, pinned. No
classes where functions work. Modules split by pipeline stage, flat tree.
`docs/index.html` is one self-contained file on purpose.

The triage prompt (`build_system`) is the product. Everything else is plumbing.
"Return fewer, never pad" is stated three ways on purpose — do not condense it.
