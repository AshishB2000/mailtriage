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
     `alice@gmail.com,alice.work@gmail.com`. A non-Gmail mailbox names its
     IMAP server after a `|` — `you@fastmail.com|imap.fastmail.com`,
     `you@work.com|mail.work.com:993` — and gets its app password from that
     provider instead (Fastmail: Settings → Privacy & Security → app
     passwords; iCloud: appleid.apple.com → App-Specific Passwords). See
     README "Other mailboxes".
   - one password secret per address, named `MAIL_PW_` + the first 16 hex
     characters (upper-cased) of a BLAKE2b-128 hash of the trimmed,
     lower-cased address — a hash, so the secret's name never shows your
     address in the public Actions log. This is exactly what `pw_env_var` in
     `src/mailtriage/imap_pull.py` computes at run time; print it for any
     address with:
     ```bash
     python3 -c 'import hashlib,sys; print("MAIL_PW_" + hashlib.blake2b(sys.argv[1].strip().lower().encode(), digest_size=16).hexdigest()[:16].upper())' alice@gmail.com
     # MAIL_PW_F24FE3C393F64986
     ```
     (The older `MAIL_PW_ALICE_GMAIL_COM`-style names — the address itself,
     upper-cased, non-alphanumerics → `_` — are deprecated but still read as
     a fallback, so a fork set up before this change keeps working.)
   - `ANTHROPIC_API_KEY` and `RESEND_API_KEY` — see the main
     [README](../README.md) for where to get these.

Run `mailtriage --self-check` locally if you want to confirm the code itself
is intact before wiring up credentials — it needs no network and no secrets.

Revoke any app password anytime from the same
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
page — it's scoped to mail access only, not your full Google account.
