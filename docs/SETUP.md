# Setup — the steps you do by hand (they need your Google password)

These involve your own Google account and produce secrets, so they're steps
only you can do — not something that can be automated for you.

1. For **each** Gmail account you want triaged, enable **2-Step
   Verification** at [myaccount.google.com/security](https://myaccount.google.com/security).
2. For **each** account, create an **app password** at
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   (choose "Mail" as the app). Copy the 16-character value — you'll only see
   it once.
3. In your fork, under **Settings → Secrets and variables → Actions**, add:
   - `MAIL_ACCOUNTS` — your addresses, comma-separated, e.g.
     `alice@gmail.com,alice.work@gmail.com`
   - one password secret per address, named by upper-casing the address and
     replacing every non-alphanumeric character with `_`, prefixed
     `MAIL_PW_` (this is exactly what `pw_env_var` in
     `src/mailtriage/imap_pull.py` computes at run time):
     - `alice@gmail.com`      → `MAIL_PW_ALICE_GMAIL_COM`
     - `alice.work@gmail.com` → `MAIL_PW_ALICE_WORK_GMAIL_COM`
   - `ANTHROPIC_API_KEY` and `RESEND_API_KEY` — see the main
     [README](../README.md) for where to get these.

Run `mailtriage --self-check` locally if you want to confirm the code itself
is intact before wiring up credentials — it needs no network and no secrets.

Revoke any app password anytime from the same
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
page — it's scoped to mail access only, not your full Google account.
