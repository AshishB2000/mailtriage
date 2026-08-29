# Setup — you do these once (they involve credentials)

Claude cannot do these; they need your Google password and produce secrets.

1. For EACH Gmail account, enable **2-Step Verification**
   (myaccount.google.com/security).
2. For EACH account, create an **app password** at
   myaccount.google.com/apppasswords (pick "Mail"). Copy the 16-char value.
3. In the routine's secret/env config, set:
   - `MAIL_ACCOUNTS` = your addresses, comma-separated
     (e.g. `alice@gmail.com,alice.work@gmail.com`)
   - one password var per address, named by upper-casing the address and
     replacing every non-alphanumeric char with `_`, prefixed `MAIL_PW_`:
     - `alice@gmail.com`      → `MAIL_PW_ALICE_GMAIL_COM`
     - `alice.work@gmail.com` → `MAIL_PW_ALICE_WORK_GMAIL_COM`
   Run `python mailtriage/imap_pull.py --self-check` locally if unsure the
   script is intact; it needs no credentials.

Revoke any app password anytime from the same page — it's mail-scoped.
