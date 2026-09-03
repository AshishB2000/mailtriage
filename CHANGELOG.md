# Changelog

All notable changes to mailtriage are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (a change to the config or secret
contract is a major bump).

## [Unreleased]

### Added

- **Any IMAP mailbox, not just Gmail.** A `MAIL_ACCOUNTS` entry may name its
  server -- `you@fastmail.com|imap.fastmail.com`, `you@work.com|mail.work.com:993`,
  or with an SMTP host as a third field -- and the engine reads `CAPABILITY`
  once per connection to pick how it stores a label: Gmail labels, IMAP
  keywords (`mailtriage/action` becomes the `$MailtriageAction` tag your
  client shows), or a `mailtriage/action` folder entered with `MOVE`. Thread
  context falls back to `References`/`In-Reply-To`, `\All` to INBOX plus
  `\Archive`, archiving to a `MOVE`, and digest links to the account's own
  webmail. `delivery: mailbox` sends the digest through that mailbox's own
  SMTP. `--doctor` reports the detected mode per account. Gmail behaviour,
  the `MAIL_PW_<HASH>` secret names and `delivery: gmail` are unchanged; see
  README "Other mailboxes" for what works where.
- **Inbox intelligence.** The model now sees up to 2 earlier messages of a
  candidate's thread (`thread_context`), its attachment names and types, and
  how often you've written to that sender in the last 180 days
  (`sender_memory`); drafts learn your voice from your own Sent mail to the
  same recipient (`draft_style.learn_voice`) and can come as a short and a
  full variant (`draft_variants: 2`); the digest ends with a folded "Noise
  this week" footer of Unsubscribe links (`show_unsubscribe`); and the
  opt-in `noise: {label, archive}` tags or archives what a run leaves out
  -- by label only, never a delete. All read-only apart from that opt-in,
  and the Actions log only ever shows counts.
- **Bring the AI you already pay for.** Six triage backends behind one
  `provider` setting in `config.yaml`: Claude subscription
  (`CLAUDE_CODE_OAUTH_TOKEN`), Claude API (`ANTHROPIC_API_KEY`), ChatGPT
  subscription via the Codex CLI (`CODEX_AUTH_JSON`), OpenAI API
  (`OPENAI_API_KEY`), Gemini API (`GEMINI_API_KEY`, free tier covers two runs
  a day), and a free Google account via the Gemini CLI (`GEMINI_OAUTH_JSON`).
  `provider: auto` picks the first secret that is set; `model` overrides each
  backend's default.
- **Reply drafting.** Every `needs_action` email gets a model-drafted reply,
  shown in the digest and appended — never sent — to that account's Gmail
  Drafts, threaded to the original. `draft_replies: false` turns it off;
  `draft_style` sets tone, sign-off, language, and length.
- **Your schedule.** `run_at` (any number of `HH:MM` slots) and `timezone`
  (IANA) in `config.yaml`; the workflow polls hourly and `mailtriage --due`
  decides whether this hour is one of yours. `window_hours` is auto-derived
  from the gaps between slots by the setup wizard.
- **Weekly review.** `weekly_review: "<day> HH:MM"` sends a once-a-week
  summary of what was surfaced and what is still open (`mailtriage --weekly`).
- **Gmail as memory.** `carry_over: true` labels every `needs_action` message
  in Gmail (`label`, default `mailtriage/action`) and keeps re-listing it
  under "Still waiting on you" until you reply, archive, or unlabel it — no
  state stored anywhere but your inbox.
- **VIP rules.** `rules.always_ignore` / `always_surface` / `always_action`
  by address or `@domain`, applied deterministically around the model, never
  inside the prompt.
- **Per-account settings.** `accounts:` overrides `interests`, `avoid`, and
  `draft_style` per Gmail address.
- **Setup wizard**: provider picker (writes exactly one AI secret and an
  explicit `provider:`), delivery picker (Gmail or Resend), timezone +
  run-time picker, weekly review, rules, draft style, per-account overrides,
  and carry-over controls; reopening it reloads all of these from your
  existing `config.yaml`.
- Run logs now report candidate/account counts and the model's returned vs.
  validated item counts — counts only, never subjects, senders, or notes —
  so a "kept none" run can be debugged from a public log.
- Hashed app-password secret names: `MAIL_PW_` + the first 16 hex characters
  of a BLAKE2b-128 hash of the address, so a secret's name no longer reveals
  the address in a public fork's Actions log. The old address-derived names
  are deprecated but still read as a fallback.
- `.github/workflows/upstream-sync.yml`: a monthly (and on-demand) pull
  request into forks carrying upstream changes, keeping the fork's
  `config.yaml` wherever it differs.
- `.github/dependabot.yml` for GitHub Actions; every `uses:` is pinned to a
  full commit SHA.
- This changelog.

### Changed

- Shipped `window_hours` default is 15 (was 13) to cover the default
  `run_at` slots' overnight gap; `config.yaml` warns (never fails) when
  `window_hours` is shorter than the largest gap between slots.
- The digest workflow runs hourly at `:17` with a schedule gate, instead of
  two fixed UTC crons — pick your times in `config.yaml`, not in the workflow.
- The Claude CLI backend uses the one invocation that has delivered a live
  digest (`claude -p … --output-format json --json-schema …`): no `--model`
  unless `model:` is set, no separate `--system-prompt`. The Codex and Gemini
  CLIs likewise pass no model unless one is configured.
- Re-running the setup wizard no longer flips a `delivery: gmail` fork to
  Resend, and leaves any secret whose field you left blank untouched.

### Fixed

- Subscription-CLI runs that returned zero items from a full inbox.
- Test fixtures use `+00:00` offsets, not `Z`, so the suite passes on
  Python 3.10 (the supported floor).

## [1.0.0] - 2026-08-29

### Added

- Twice-daily AI triage of one or more Gmail inboxes over read-only IMAP,
  sorted into **needs action** / **worth reading** with the noise dropped,
  delivered as one short HTML email; a quiet window sends nothing.
- Two AI-auth modes: an Anthropic API key, or a Claude Pro/Max subscription
  via `CLAUDE_CODE_OAUTH_TOKEN` — chosen by whichever secret is set.
- Two delivery modes: `gmail` (your own Gmail over SMTP, reusing the app
  password you read with) and `email` (Resend, from a verified domain).
- Zero-backend setup wizard (`docs/index.html`): encrypts secrets in the
  browser with libsodium, writes them to your fork's Actions secrets, commits
  `config.yaml`, and starts the first run — reopens as a settings editor.
- `EMAIL_TO` / `EMAIL_FROM` live in secrets, not the committed `config.yaml`,
  so a public fork never carries an address.
- Runs on GitHub Actions with a self-check before any API spend; a failed
  account warns and the run continues.
- Contract tests pinning the wizard/engine mirrors, actionlint in CI, and
  project skills for Claude Code.

[Unreleased]: https://github.com/AshishB2000/mailtriage/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/AshishB2000/mailtriage/releases/tag/v1.0.0
