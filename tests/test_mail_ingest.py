"""Mail ingestion, driven against a fake IMAP object.

The fake implements the two imaplib methods the poll uses (select, uid), so
the whole loop runs for real — message parsing, state tracking, atomic
saves — with no network and no Gmail account.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from email.message import EmailMessage

from mail_ingest import (MailError, load_config, load_state, pdf_attachments,
                         poll_once, sanitise, save_state)

PDF = b"%PDF-1.1\n%%EOF\n"


def make_message(*attachments: tuple[str, bytes, str],
                 body: str = "see attached") -> bytes:
    message = EmailMessage()
    message["Subject"] = "Fwd: CCMP referral"
    message["From"] = "reception@yourclinic.com.au"
    message["To"] = "admin+referral@yourclinic.com.au"
    message.set_content(body)
    for filename, payload, mime in attachments:
        maintype, _, subtype = mime.partition("/")
        message.add_attachment(payload, maintype=maintype, subtype=subtype,
                               filename=filename)
    return bytes(message)


class FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL for poll_once."""

    def __init__(self, messages: dict[str, bytes], label: str = "Referrals"):
        self.messages = messages
        self.label = label
        self.fetched: list[str] = []

    def select(self, mailbox: str, readonly: bool = False):
        assert readonly, "the mailbox must always be opened read-only"
        if mailbox.strip('"') != self.label:
            return "NO", [b"no such mailbox"]
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command: str, *args):
        if command == "search":
            uids = " ".join(sorted(self.messages, key=int)).encode()
            return "OK", [uids]
        if command == "fetch":
            uid = args[0]
            self.fetched.append(uid)
            raw = self.messages.get(uid)
            if raw is None:
                return "OK", [None]
            return "OK", [(f"{uid} (RFC822)".encode(), raw)]
        raise AssertionError(f"unexpected command {command}")


class IngestTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {
            "label": "Referrals",
            "inbox_dir": os.path.join(self.tmp.name, "inbox"),
            "state_file": os.path.join(self.tmp.name, "mail_state.json"),
        }

    def state(self) -> dict:
        return load_state(self.config["state_file"])

    def inbox_files(self) -> list[str]:
        path = self.config["inbox_dir"]
        return sorted(os.listdir(path)) if os.path.isdir(path) else []


class AttachmentTests(unittest.TestCase):
    def test_pdf_attachment_is_found(self):
        raw = make_message(("A. Ward.pdf", PDF, "application/pdf"))
        [(name, payload)] = pdf_attachments(raw)
        self.assertEqual(name, "A. Ward.pdf")
        self.assertEqual(payload, PDF)

    def test_pdf_typed_as_octet_stream_is_still_found(self):
        """Fax gateways and some GP software send PDFs as octet-stream —
        the .pdf filename has to be enough."""
        raw = make_message(("referral.pdf", PDF, "application/octet-stream"))
        self.assertEqual(len(pdf_attachments(raw)), 1)

    def test_non_pdf_attachments_are_ignored(self):
        raw = make_message(("photo.jpg", b"\xff\xd8\xff", "image/jpeg"),
                           ("notes.docx", b"PK", "application/vnd.ms-word"))
        self.assertEqual(pdf_attachments(raw), [])

    def test_message_without_attachments_yields_nothing(self):
        self.assertEqual(pdf_attachments(make_message()), [])

    def test_multiple_pdfs_in_one_email(self):
        raw = make_message(("page1.pdf", PDF, "application/pdf"),
                           ("page2.pdf", PDF + b"2", "application/pdf"))
        self.assertEqual(len(pdf_attachments(raw)), 2)


class SanitiseTests(unittest.TestCase):
    def test_plain_name_kept(self):
        self.assertEqual(sanitise("A. Ward.pdf"), "A. Ward.pdf")

    def test_path_traversal_is_stripped(self):
        for hostile in ("../../etc/passwd.pdf", "..\\..\\evil.pdf",
                        "/etc/shadow.pdf"):
            with self.subTest(name=hostile):
                cleaned = sanitise(hostile)
                self.assertNotIn("/", cleaned)
                self.assertNotIn("\\", cleaned)
                self.assertFalse(cleaned.startswith("."))

    def test_empty_name_gets_a_default(self):
        self.assertEqual(sanitise(""), "referral.pdf")


class PollTests(IngestTestCase):
    def test_saves_pdfs_and_remembers_the_uid(self):
        imap = FakeIMAP({"101": make_message(
            ("A. Ward.pdf", PDF, "application/pdf"))})
        saved = poll_once(imap, self.config, self.state())
        self.assertEqual(len(saved), 1)
        self.assertEqual(self.inbox_files(), ["101-A. Ward.pdf"])
        self.assertEqual(self.state()["seen_uids"], ["101"])

    def test_second_poll_fetches_nothing(self):
        imap = FakeIMAP({"101": make_message(
            ("A. Ward.pdf", PDF, "application/pdf"))})
        poll_once(imap, self.config, self.state())
        imap.fetched.clear()
        saved = poll_once(imap, self.config, self.state())
        self.assertEqual(saved, [])
        self.assertEqual(imap.fetched, [])

    def test_message_without_pdfs_is_still_marked_seen(self):
        imap = FakeIMAP({"7": make_message(body="no attachment here")})
        poll_once(imap, self.config, self.state())
        self.assertEqual(self.state()["seen_uids"], ["7"])
        self.assertEqual(self.inbox_files(), [])

    def test_failed_fetch_is_retried_next_poll(self):
        """A UID that fails to download must NOT be marked seen — that
        would silently drop a referral."""
        imap = FakeIMAP({"9": make_message(
            ("r.pdf", PDF, "application/pdf"))})
        real_uid = imap.uid

        def flaky(command, *args):
            if command == "fetch":
                return "OK", [None]
            return real_uid(command, *args)

        imap.uid = flaky
        poll_once(imap, self.config, self.state())
        self.assertEqual(self.state()["seen_uids"], [])
        imap.uid = real_uid
        saved = poll_once(imap, self.config, self.state())
        self.assertEqual(len(saved), 1)

    def test_wrong_label_is_a_clear_error(self):
        imap = FakeIMAP({}, label="SomethingElse")
        with self.assertRaises(MailError) as ctx:
            poll_once(imap, self.config, self.state())
        self.assertIn("Referrals", str(ctx.exception))

    def test_state_survives_on_disk_between_polls(self):
        imap = FakeIMAP({"3": make_message(
            ("a.pdf", PDF, "application/pdf"))})
        poll_once(imap, self.config, self.state())
        with open(self.config["state_file"], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["seen_uids"], ["3"])


class ConfigTests(unittest.TestCase):
    def test_missing_file_says_what_to_do(self):
        with self.assertRaises(MailError) as ctx:
            load_config("/nonexistent/mail_config.json")
        self.assertIn("mail_config.example.json", str(ctx.exception))

    def test_missing_app_password_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mail_config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"user": "a@b.c"}, f)
            with self.assertRaises(MailError) as ctx:
                load_config(path)
            self.assertIn("app_password", str(ctx.exception))

    def test_defaults_fill_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mail_config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"user": "a@b.c", "app_password": "x"}, f)
            config = load_config(path)
            self.assertEqual(config["host"], "imap.gmail.com")
            self.assertEqual(config["label"], "Referrals")

    def test_example_config_is_valid_apart_from_the_placeholder(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config = load_config(os.path.join(root, "mail_config.example.json"))
        self.assertIn("PASTE", config["app_password"])


class StateRoundTripTests(unittest.TestCase):
    def test_corrupt_state_file_resets_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(load_state(path), {"seen_uids": []})

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            save_state(path, {"seen_uids": ["1", "2"]})
            self.assertEqual(load_state(path)["seen_uids"], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
