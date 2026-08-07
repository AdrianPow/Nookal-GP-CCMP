"""The review screen, driven over real HTTP against a fake Nookal.

Builds queue items directly rather than from PDFs, so these run without
pdfplumber, pypdf or Tesseract.
"""

from __future__ import annotations

import os
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

import review_server as rs
from referral_pipeline import BLOCKED, DONE, NEEDS_PAYER, Queue, ReviewItem

from .base import ClientTestCase
from .fake_nookal import success


class ServerTestCase(ClientTestCase):
    def setUp(self):
        super().setUp()
        self.queue = Queue(os.path.join(self.tmp.name, "queue"))
        self.pdf_path = os.path.join(self.tmp.name, "referral.pdf")
        with open(self.pdf_path, "wb") as f:
            f.write(b"%PDF-1.1\n%%EOF\n")
        self.fake.routes_from({
            "searchPatients": success({"patients": []}),
            "addPatient": success({"patient": {"patient_id": 2521}}),
            "updatePatientMedicareDetails": success({"ok": True}),
            "addCase": success({"cases": [{"ID": "3466"}]}),
            "uploadFile": lambda p: success(
                {"url": self.fake.s3_url(), "file_id": "f1"}),
            "setFileActive": success({"file_id": "f1"}),
        })

    def start(self, client=None, directory=None):
        server = rs.ReviewServer(("127.0.0.1", 0), rs.Handler)
        server.queue = self.queue
        server.client = client
        server.directory = directory
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def add(self, item_id="abc", **fields) -> ReviewItem:
        base = {"patient_name": "Aiden Ward", "dob": "1960-05-04"}
        base.update(fields)
        item = ReviewItem(
            id=item_id, pdf_path=self.pdf_path,
            received="2026-08-03T09:00:00",
            fields={k: v for k, v in base.items() if v is not None},
            confidence={"medicare_no": "ok", "dob": "check"})
        self.queue.save(item)
        return item

    def get(self, base, path):
        with urllib.request.urlopen(base + path) as r:
            return r.read().decode()

    def post(self, base, path, data):
        req = urllib.request.Request(
            base + path, data=urllib.parse.urlencode(data).encode(),
            method="POST")
        with urllib.request.urlopen(req) as r:
            return r.read().decode()


class ListTests(ServerTestCase):
    def test_empty_queue_says_so(self):
        base = self.start()
        self.assertIn("Nothing in the queue", self.get(base, "/"))

    def test_items_are_listed_with_a_link(self):
        self.add()
        base = self.start()
        body = self.get(base, "/")
        self.assertIn("Aiden Ward", body)
        self.assertIn("/r/abc", body)

    def test_unknown_item_is_a_404_not_a_crash(self):
        base = self.start()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get(base, "/r/nope")
        self.assertEqual(ctx.exception.code, 404)


class DetailTests(ServerTestCase):
    def test_shows_fields_and_a_pdf_link(self):
        self.add(medicare_no="4070953263")
        base = self.start()
        body = self.get(base, "/r/abc")
        self.assertIn("4070953263", body)
        self.assertIn("/r/abc/pdf", body)

    def test_pdf_is_served(self):
        self.add()
        base = self.start()
        with urllib.request.urlopen(base + "/r/abc/pdf") as r:
            self.assertEqual(r.read()[:4], b"%PDF")
            self.assertEqual(r.headers["Content-Type"], "application/pdf")

    def test_missing_session_count_uses_the_standard_five(self):
        """Clinic policy: the patient tracks their own entitlement, so a
        silent referral gets the standard 5 without ceremony."""
        self.add(services_count=None)
        base = self.start()
        body = self.get(base, "/r/abc")
        self.assertIn("value='5'", body)
        self.assertIn("using the standard 5", body)

    def test_stated_count_is_shown_not_replaced_by_the_default(self):
        self.add(services_count=1)
        base = self.start()
        body = self.get(base, "/r/abc")
        self.assertIn("value='1'", body)
        self.assertNotIn("using the standard", body)

    def test_stated_session_count_does_not_warn(self):
        self.add(services_count=5)
        base = self.start()
        self.assertNotIn("Not stated on the referral",
                         self.get(base, "/r/abc"))

    def test_names_are_escaped(self):
        self.add(patient_name="Ward & <script>alert(1)</script>")
        base = self.start()
        body = self.get(base, "/r/abc")
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)


class SaveTests(ServerTestCase):
    def test_edits_are_kept(self):
        self.add()
        base = self.start()
        self.post(base, "/r/abc/save",
                  {"patient_name": "Aiden Ward", "dob": "1960-05-04",
                   "medicare_no": "4070953263", "services_count": "5"})
        item = self.queue.get("abc")
        self.assertEqual(item.fields["medicare_no"], "4070953263")
        self.assertEqual(item.fields["services_count"], 5)

    def test_saving_alone_writes_nothing_to_nookal(self):
        self.add()
        base = self.start(self.make_client())
        self.post(base, "/r/abc/save", {"patient_name": "Aiden Ward",
                                        "dob": "1960-05-04"})
        self.assertEqual(self.fake.sequence(), [])

    def test_clearing_a_field_removes_it(self):
        self.add(medicare_no="4070953263")
        base = self.start()
        self.post(base, "/r/abc/save", {"patient_name": "Aiden Ward",
                                        "dob": "1960-05-04",
                                        "medicare_no": ""})
        self.assertNotIn("medicare_no", self.queue.get("abc").fields)


class CreateTests(ServerTestCase):
    def test_create_runs_the_sequence_and_moves_state(self):
        self.add()
        base = self.start(self.make_client())
        self.post(base, "/r/abc/create",
                  {"patient_name": "Aiden Ward", "dob": "1960-05-04",
                   "medicare_no": "4070953263", "services_count": "5"})
        item = self.queue.get("abc")
        self.assertEqual(item.state, NEEDS_PAYER)
        self.assertEqual(item.nookal["patient_id"], 2521)
        self.assertEqual(item.nookal["case_id"], 3466)
        self.assertIn("addCase", self.fake.endpoints())

    def test_created_screen_gives_the_payer_values(self):
        self.add(gp_name="Dr Kym R. Horsnell",
                 gp_provider_number="040501AW")
        base = self.start(self.make_client())
        self.post(base, "/r/abc/create",
                  {"patient_name": "Aiden Ward", "dob": "1960-05-04",
                   "gp_name": "Dr Kym R. Horsnell",
                   "gp_provider_number": "040501AW", "services_count": "5"})
        body = self.get(base, "/r/abc")
        self.assertIn("Add Payer", body)
        self.assertIn("040501AW", body)
        self.assertIn("Dr Kym R. Horsnell", body)

    def test_no_payer_is_created_through_the_screen(self):
        self.add()
        base = self.start(self.make_client())
        self.post(base, "/r/abc/create", {"patient_name": "Aiden Ward",
                                          "dob": "1960-05-04"})
        self.assertEqual(self.fake.count("editCasePayer"), 0)

    def test_without_a_nookal_connection_it_blocks_and_explains(self):
        self.add()
        base = self.start(client=None)
        self.post(base, "/r/abc/create", {"patient_name": "Aiden Ward",
                                          "dob": "1960-05-04"})
        item = self.queue.get("abc")
        self.assertEqual(item.state, BLOCKED)
        self.assertIn("No Nookal connection", item.message)

    def test_missing_dob_blocks_without_writing(self):
        self.add()
        base = self.start(self.make_client())
        self.post(base, "/r/abc/create", {"patient_name": "Aiden Ward",
                                          "dob": ""})
        self.assertEqual(self.queue.get("abc").state, BLOCKED)
        self.assertEqual(self.fake.sequence(), [])

    def test_dry_run_creates_nothing(self):
        self.add()
        base = self.start(self.make_client(dry_run=True))
        self.post(base, "/r/abc/create", {"patient_name": "Aiden Ward",
                                          "dob": "1960-05-04"})
        for endpoint in ("addPatient", "addCase", "uploadFile"):
            self.assertEqual(self.fake.count(endpoint), 0)

    def test_create_teaches_the_practice_directory(self):
        """The name the reviewer approved — corrections included — is
        stored against the referral's practice keys, so the next referral
        from the same practice starts out right."""
        from practice_directory import PracticeDirectory
        item = self.add(gp_practice="bk MEDICAL CENTRE P: 073886 5100")
        item.practice_keys = {"phones": ["0738865100"], "domains": [],
                              "provider_numbers": [], "addresses": []}
        self.queue.save(item)
        directory = PracticeDirectory(
            os.path.join(self.tmp.name, "practices.json"))
        base = self.start(self.make_client(), directory=directory)
        self.post(base, "/r/abc/create",
                  {"patient_name": "Aiden Ward", "dob": "1960-05-04",
                   "gp_practice": "Castle Hill Medical Centre"})
        match = directory.lookup({"phones": ["0738865100"], "domains": [],
                                  "provider_numbers": [], "addresses": []})
        self.assertEqual(match.name, "Castle Hill Medical Centre")

    def test_saving_for_later_teaches_nothing(self):
        """Only Create is a confirmation. A save may be a half-checked
        form the reviewer is coming back to."""
        from practice_directory import PracticeDirectory
        item = self.add(gp_practice="Some Practice")
        item.practice_keys = {"phones": ["0738865100"], "domains": [],
                              "provider_numbers": [], "addresses": []}
        self.queue.save(item)
        directory = PracticeDirectory(
            os.path.join(self.tmp.name, "practices.json"))
        base = self.start(self.make_client(), directory=directory)
        self.post(base, "/r/abc/save", {"gp_practice": "Some Practice"})
        self.assertEqual(directory.records, [])

    def test_marking_the_payer_done_verifies_it_first(self):
        """'Done' is not taken on trust — the payer is read back, because
        it is the one step the API cannot write."""
        self.add(services_count=5)
        base = self.start(self.make_client())
        self.post(base, "/r/abc/create", {"patient_name": "Aiden Ward",
                                          "dob": "1960-05-04",
                                          "services_count": "5"})
        self.fake.route("getCases", success({"cases": [
            {"ID": "3466", "payers": [{"ID": "962", "payer": "Medicare",
                                       "Sessions_Approved": "5",
                                       "Sessions_Completed": "0"}]}]}))
        self.post(base, "/r/abc/done", {})
        self.assertEqual(self.queue.get("abc").state, DONE)


if __name__ == "__main__":
    unittest.main()
