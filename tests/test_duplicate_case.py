"""Not creating a second GP CCMP case on a patient who already has one.

A patient can legitimately be referred again, so this is a stop-and-ask,
not a refusal. But re-processing a referral that was already done is far
more likely than a genuine second referral — especially while testing with
archived referrals, where every patient is already in Nookal.
"""

from __future__ import annotations

import os
import threading
import unittest
import urllib.parse
import urllib.request

import review_server as rs
from referral_pipeline import (BLOCKED, NEEDS_PAYER, Queue, ReviewItem,
                               create_in_nookal, existing_ccmp_cases)

from .base import ClientTestCase
from .fake_nookal import failure, success


def case(case_id="3466", title="GP CCMP"):
    return {"ID": case_id, "caseTitle": title, "patientID": "2521",
            "payers": []}


class ExistingCaseTests(ClientTestCase):
    def test_finds_a_ccmp_case(self):
        self.fake.route("getCases", success({"cases": [case()]}))
        found = existing_ccmp_cases(self.make_client(), 2521)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["ID"], "3466")

    def test_other_case_titles_are_ignored(self):
        self.fake.route("getCases", success({"cases": [
            case("1", "Physio"), case("2", "General")]}))
        self.assertEqual(existing_ccmp_cases(self.make_client(), 2521), [])

    def test_title_match_is_exact(self):
        """A near-miss title is a different bucket of cases, not this one."""
        self.fake.route("getCases", success({"cases": [
            case("1", "GP CCMP 2025"), case("2", "gp ccmp")]}))
        self.assertEqual(existing_ccmp_cases(self.make_client(), 2521), [])

    def test_a_failed_lookup_never_blocks(self):
        """If the case list cannot be read, that must not stop a create —
        failing to file a referral is worse than a possible duplicate."""
        self.fake.route("getCases", failure("service unavailable"))
        self.assertEqual(existing_ccmp_cases(self.make_client(), 2521), [])


class CreateGuardTests(ClientTestCase):
    def routes(self, cases):
        self.fake.routes_from({
            "searchPatients": success({"patients": [{"patient_id": 2521}]}),
            "getCases": success({"cases": cases}),
            "addCase": success({"cases": [{"ID": "9999"}]}),
            "updatePatientMedicareDetails": success({"ok": True}),
            "uploadFile": lambda p: success(
                {"url": self.fake.s3_url(), "file_id": "f1"}),
            "setFileActive": success({"file_id": "f1"}),
        })

    def item(self, **kw) -> ReviewItem:
        pdf = os.path.join(self.tmp.name, "r.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.1\n%%EOF\n")
        return ReviewItem(id="abc", pdf_path=pdf,
                          received="2026-08-04T09:00:00",
                          fields={"patient_name": "Aiden Ward",
                                  "dob": "1960-05-04"}, **kw)

    def test_existing_case_blocks_and_names_it(self):
        self.routes([case("3466")])
        result = create_in_nookal(self.make_client(), self.item())
        self.assertFalse(result.ok)
        self.assertEqual(result.state, BLOCKED)
        self.assertIn("3466", result.message)
        self.assertEqual(self.fake.count("addCase"), 0)

    def test_an_exhausted_previous_plan_is_spelled_out(self):
        """A patient referred again next year is the normal reason for a
        second case. Whether the old plan is used up is the deciding fact,
        so it goes in the message rather than making someone go and look."""
        used_up = dict(case("3466"), payers=[{
            "ID": "962", "payer": "Medicare", "Sessions_Approved": "5",
            "Sessions_Completed": "5", "ReferralDate": "2025-07-16"}])
        self.routes([used_up])
        result = create_in_nookal(self.make_client(), self.item())
        self.assertIn("5 of 5 sessions used", result.message)
        self.assertIn("referred 2025-07-16", result.message)
        self.assertIn("Create anyway", result.message)

    def test_a_plan_still_in_use_is_equally_visible(self):
        part_used = dict(case("3466"), payers=[{
            "ID": "962", "Sessions_Approved": "5",
            "Sessions_Completed": "2", "ReferralDate": "2026-07-01"}])
        self.routes([part_used])
        result = create_in_nookal(self.make_client(), self.item())
        self.assertIn("2 of 5 sessions used", result.message)

    def test_placeholder_referral_dates_are_not_shown(self):
        """Nookal stores an unset date as 0000-00-00."""
        blank = dict(case("3466"), payers=[{
            "ID": "962", "Sessions_Approved": "5",
            "Sessions_Completed": "0", "ReferralDate": "0000-00-00"}])
        self.routes([blank])
        result = create_in_nookal(self.make_client(), self.item())
        self.assertNotIn("0000", result.message)

    def test_no_existing_case_proceeds(self):
        self.routes([case("1", "Physio")])
        result = create_in_nookal(self.make_client(), self.item())
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.state, NEEDS_PAYER)

    def test_override_lets_a_genuine_second_referral_through(self):
        self.routes([case("3466")])
        result = create_in_nookal(self.make_client(),
                                  self.item(allow_duplicate_case=True))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.fake.count("addCase"), 1)

    def test_a_rehearsal_is_not_blocked_by_it(self):
        """Dry runs are for demonstrating the flow, usually with archived
        referrals whose patients all already have cases. Blocking there
        would make the rehearsal useless and writes nothing anyway."""
        self.routes([case("3466")])
        result = create_in_nookal(self.make_client(dry_run=True), self.item())
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.fake.count("addCase"), 0)


class CreateAnywayButtonTests(ClientTestCase):
    def setUp(self):
        super().setUp()
        self.queue = Queue(os.path.join(self.tmp.name, "queue"))
        pdf = os.path.join(self.tmp.name, "r.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.1\n%%EOF\n")
        self.queue.save(ReviewItem(
            id="abc", pdf_path=pdf, received="2026-08-04T09:00:00",
            fields={"patient_name": "Aiden Ward", "dob": "1960-05-04"}))
        self.fake.routes_from({
            "searchPatients": success({"patients": [{"patient_id": 2521}]}),
            "getCases": success({"cases": [case("3466")]}),
            "addCase": success({"cases": [{"ID": "9999"}]}),
            "uploadFile": lambda p: success(
                {"url": self.fake.s3_url(), "file_id": "f1"}),
            "setFileActive": success({"file_id": "f1"}),
        })
        server = rs.ReviewServer(("127.0.0.1", 0), rs.Handler)
        server.queue = self.queue
        server.client = self.make_client()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(self, path, data=None):
        req = urllib.request.Request(
            self.base + path,
            data=urllib.parse.urlencode(data or {}).encode(), method="POST")
        with urllib.request.urlopen(req) as r:
            return r.read().decode()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.read().decode()

    def fields(self):
        return {"patient_name": "Aiden Ward", "dob": "1960-05-04"}

    def test_create_blocks_then_offers_the_override(self):
        self.post("/r/abc/create", self.fields())
        item = self.queue.get("abc")
        self.assertEqual(item.state, BLOCKED)
        self.assertIn("already has a GP CCMP case", item.message)
        self.assertIn("Create anyway", self.get("/r/abc"))

    def test_create_anyway_goes_through(self):
        self.post("/r/abc/create", self.fields())
        self.post("/r/abc/create_anyway", self.fields())
        item = self.queue.get("abc")
        self.assertEqual(item.state, NEEDS_PAYER)
        self.assertEqual(self.fake.count("addCase"), 1)

    def test_the_override_is_not_offered_by_default(self):
        self.assertNotIn("Create anyway", self.get("/r/abc"))


if __name__ == "__main__":
    unittest.main()
