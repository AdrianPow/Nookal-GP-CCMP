# Getting referrals from Gmail into the review screen

For Google Workspace. Staff forward a referral email; a filter labels it;
the worker on the clinic PC saves the PDF into `inbox/`; the review screen
picks it up within a minute.

No extra mailbox licence is needed — this uses plus-addressing on the
account you already have.

## 1. Pick the forwarding address

Gmail delivers anything sent to `admin+anything@yourclinic.com.au` straight
to `admin@yourclinic.com.au`. Nothing to create or pay for.

Use **`admin+referral@yourclinic.com.au`**. That is the only new thing
staff have to learn, and it works from a phone.

## 2. Make the label and the filter

In Gmail on the web, as the account owner:

1. **Settings** (gear, top right) → **See all settings** → **Filters and
   Blocked Addresses** → **Create a new filter**.
2. In **To**, enter `admin+referral@yourclinic.com.au`.
3. Tick **Has attachment**.
4. Click **Create filter**.
5. Tick **Apply the label** → **New label…** → name it exactly
   `Referrals` → **Create**.
6. Leave everything else unticked. In particular **do not** tick "Skip the
   Inbox" — the email staying in the inbox is a useful backstop if the PC
   is off.
7. Click **Create filter**.

The label name must match `"label"` in `mail_config.json` character for
character, including the capital R.

## 3. Create an app password

The worker signs in with an app password, not the account password. It only
grants mail access and can be revoked on its own without changing anyone's
login.

1. Go to **myaccount.google.com** → **Security**.
2. **2-Step Verification** must be on. If it isn't, turn it on first —
   Google will not offer app passwords otherwise.
3. Search the settings page for **App passwords** (or go to
   myaccount.google.com/apppasswords).
4. Name it `Nookal referral worker` → **Create**.
5. Copy the 16-character password. **Google shows it once.**

If **App passwords** does not appear, a Workspace admin has disabled them.
In the Admin console: **Security → Access and data control → Less secure
apps / App passwords**. Ask your admin to allow them for this account.

## 4. Turn IMAP on

Gmail settings → **Forwarding and POP/IMAP** → **Enable IMAP** → **Save
Changes**. Workspace accounts sometimes have this off by default.

## 5. Configure the worker

On the clinic PC, in the project folder:

```bash
cp mail_config.example.json mail_config.json
open -e mail_config.json
```

Fill in `user` and `app_password` (spaces in the app password are fine —
paste it as Google showed it). Save and close.

`mail_config.json` is gitignored. It holds a working credential, so it must
never be committed or emailed.

## 6. Test it

Forward a referral to `admin+referral@yourclinic.com.au`, wait for it to
arrive, then:

```bash
.venv/bin/python mail_ingest.py --once
```

Expect:

```
[14:32:07] saved 1 PDF(s):
           inbox/1183-A. Ward.pdf
```

Then start the review screen and the PDF will be waiting:

```bash
.venv/bin/python review_server.py --dry-run
```

## 7. Run it continuously

```bash
.venv/bin/python mail_ingest.py
```

Polls every two minutes and prints what it finds. Leave it running in its
own Terminal window alongside `review_server.py`. (Starting both on boot is
the next piece of work.)

## What it will and won't do

**It never modifies the mailbox.** The connection is read-only: nothing is
marked read, moved, archived or deleted. The original email stays exactly
where staff can see it.

**A referral cannot be silently lost.** Which messages have been processed
is tracked in `mail_state.json`. If a download fails, that message is *not*
marked done and is retried on the next poll. If the PC dies and you set it
up fresh, it re-reads the whole label and catches up — and duplicates are
dropped by content hash, so nothing appears twice.

**Non-PDF attachments are ignored**, and PDFs sent as `octet-stream` (some
fax gateways do this) are still picked up on the `.pdf` filename.

**The same referral forwarded twice** creates one queue item, not two.

## If something goes wrong

| Message | Cause |
|---|---|
| `could not open label 'Referrals'` | The label doesn't exist, or the spelling in `mail_config.json` differs |
| `AUTHENTICATIONFAILED` | Wrong app password, or you used the account password |
| `... is missing 'app_password'` | `mail_config.json` not filled in |
| Nothing saved, no error | The filter isn't applying the label — check a forwarded email actually shows it |
