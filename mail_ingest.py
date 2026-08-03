#!/usr/bin/env python3
"""
mail_ingest.py — pull referral PDFs out of a Gmail mailbox into inbox/.

Built for Google Workspace. Staff forward a referral email to a
plus-address on the existing account (e.g. admin+referral@yourclinic), a
Gmail filter applies the label configured below, and this worker polls that
label over IMAP and saves every PDF attachment into the inbox folder the
review screen watches.

The mailbox is never modified: the connection is read-only, nothing is
marked read, moved or deleted. Which messages have already been ingested is
tracked locally in a state file — if this PC ever dies, a fresh install
re-reads the whole label and catches up, and duplicate PDFs are dropped by
the review screen's content-hash check.

Config (mail_config.json — gitignored, it holds the app password):

    {
      "user":         "admin@yourclinic.com.au",
      "app_password": "abcd efgh ijkl mnop",
      "label":        "Referrals",
      "host":         "imap.gmail.com",
      "inbox_dir":    "inbox",
      "state_file":   "mail_state.json",
      "poll_seconds": 120
    }

The app password is NOT the account password: Google Account -> Security ->
2-Step Verification -> App passwords. It only grants mail access and can be
revoked on its own.

    python3 mail_ingest.py --once     # single poll, then exit
    python3 mail_ingest.py            # poll forever
"""

from __future__ import annotations

import argparse
import email
import email.policy
import imaplib
import json
import os
import re
import time
from typing import Any, Iterable

CONFIG_DEFAULTS = {
    "host": "imap.gmail.com",
    "label": "Referrals",
    "inbox_dir": "inbox",
    "state_file": "mail_state.json",
    "poll_seconds": 120,
}


class MailError(Exception):
    pass


def load_config(path: str = "mail_config.json") -> dict:
    if not os.path.exists(path):
        raise MailError(
            f"{path} not found — copy mail_config.example.json to {path} "
            "and fill in the account and app password.")
    with open(path, encoding="utf-8") as f:
        config = {**CONFIG_DEFAULTS, **json.load(f)}
    for key in ("user", "app_password"):
        if not config.get(key):
            raise MailError(f"{path} is missing {key!r}.")
    return config


# --------------------------------------------------------------------------
# Local state — which message UIDs have already been ingested
# --------------------------------------------------------------------------

def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("seen_uids", [])
        return state
    except (OSError, json.JSONDecodeError):
        return {"seen_uids": []}


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Message handling
# --------------------------------------------------------------------------

def sanitise(name: str) -> str:
    """A filename safe to write into inbox/: no paths, no surprises."""
    base = os.path.basename(str(name).replace("\\", "/")).strip()
    base = re.sub(r"[^A-Za-z0-9._ \-]", "_", base).lstrip(".")
    return base or "referral.pdf"


def pdf_attachments(raw: bytes) -> list[tuple[str, bytes]]:
    """Every PDF in a message: named .pdf, or typed application/pdf."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    found: list[tuple[str, bytes]] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename() or ""
        is_pdf = (part.get_content_type() == "application/pdf"
                  or filename.lower().endswith(".pdf"))
        if not is_pdf:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            found.append((sanitise(filename or "referral.pdf"), payload))
    return found


def save_attachment(inbox_dir: str, uid: str, name: str,
                    payload: bytes) -> str | None:
    """Write atomically, named by UID so re-polls never collide. Returns the
    path written, or None if it already exists."""
    os.makedirs(inbox_dir, exist_ok=True)
    dest = os.path.join(inbox_dir, f"{uid}-{name}")
    if os.path.exists(dest):
        return None
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(payload)
    os.replace(tmp, dest)
    return dest


# --------------------------------------------------------------------------
# The poll
# --------------------------------------------------------------------------

def poll_once(imap: Any, config: dict, state: dict) -> list[str]:
    """One pass over the label. `imap` is anything with select() and uid()
    (imaplib.IMAP4_SSL in production, a fake in the tests). Returns the
    paths of newly saved PDFs."""
    status, _ = imap.select(f'"{config["label"]}"', readonly=True)
    if status != "OK":
        raise MailError(
            f"could not open label {config['label']!r} — does the Gmail "
            "filter and label exist, and is the spelling identical?")
    status, data = imap.uid("search", None, "ALL")
    if status != "OK":
        raise MailError("UID search failed")
    uids = [u.decode() for u in (data[0].split() if data and data[0] else [])]

    seen = set(state["seen_uids"])
    saved: list[str] = []
    for uid in uids:
        if uid in seen:
            continue
        status, msgdata = imap.uid("fetch", uid, "(RFC822)")
        if status != "OK" or not msgdata or msgdata[0] is None:
            continue                      # try again next poll; not marked seen
        raw = msgdata[0][1]
        for name, payload in pdf_attachments(raw):
            path = save_attachment(config["inbox_dir"], uid, name, payload)
            if path:
                saved.append(path)
        # A message with no PDFs is still done — remember it so every poll
        # doesn't refetch the whole label.
        state["seen_uids"].append(uid)
        save_state(config["state_file"], state)
    return saved


def connect(config: dict) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(config["host"])
    imap.login(config["user"], config["app_password"])
    return imap


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--config", default="mail_config.json")
    ap.add_argument("--once", action="store_true",
                    help="poll a single time and exit")
    args = ap.parse_args()

    try:
        config = load_config(args.config)
    except MailError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1

    state = load_state(config["state_file"])
    while True:
        try:
            imap = connect(config)
            saved = poll_once(imap, config, state)
            try:
                imap.logout()
            except Exception:                          # noqa: BLE001
                pass
            stamp = time.strftime("%H:%M:%S")
            if saved:
                print(f"[{stamp}] saved {len(saved)} PDF(s):")
                for path in saved:
                    print(f"           {path}")
            else:
                print(f"[{stamp}] nothing new")
        except (MailError, imaplib.IMAP4.error, OSError) as exc:
            print(f"[{time.strftime('%H:%M:%S')}] poll failed: {exc} — "
                  "will retry")
        if args.once:
            return 0
        time.sleep(config["poll_seconds"])


if __name__ == "__main__":
    raise SystemExit(main())
