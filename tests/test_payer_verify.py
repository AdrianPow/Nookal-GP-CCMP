"""Reading the payer back after the manual step.

The payer cannot be written through the API — editCasePayer returns success
and changes nothing, verified against a real payer on 2026-08-04. Reading it
back is therefore the only check on the one step a human performs by hand,
and the failure it exists to catch is Sessions = 0, which Nookal treats as
Unlimited rather than as none.
"""

from __future__ import annotations

import os
import unittest

from referral_pipeline import (DEFAULT_SESSIONS, DONE, NEEDS_PAYER,
                               ReviewItem, verify_payer)

from .base import ClientTestCase
from .fake_nookal import failure, success


def payer(approved="5", completed="0", payer_id="962"):
    """Shaped exactly like a real Nookal payer record."""
    return {"ID": payer_id, "payer": "Medicare", "ReferralDate": "0000-00-00",
            "Reference": "", "DateOfInjury": None,
            "Sessions_Approved": approved, "Sessions_Completed": completed,
            "ExpiryDate": None, "Notes": "", "Status": "1",
            "budget": "0.00", "caseManager": "", "referrer": ""}


class VerifyPayerTests(ClientTestCase):
    def make_item(self, **fields) -> ReviewItem:
        pdf = os.path.join(self.tmp.name, "r.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.1\n%%EOF\n")
        base = {"patient_name": "Aiden Ward", "dob": "1960-05-04"}
        base.update(fields)
        return ReviewItem(
            id="abc", pdf_path=pdf, state=NEEDS_PAYER,
            received="2026-08-04T09:00:00",
            fields={k: v for k, v in base.items() if v is not None},
            nookal={"patient_id": 2521, "case_id": 3466})

    def route_case(self, payers):
        self.fake.route("getCases", success({"cases": [
            {"ID": "3466", "caseTitle": "GP CCMP", "patientID": "2521",
             "payers": payers},
            {"ID": "3469", "caseTitle": "OTHER", "payers": []},
        ]}))

    # ------------------------------------------------------------ passing

    def test_matching_count_passes(self):
        self.route_case([payer(approved="5")])
        check = verify_payer(self.make_client(), self.make_item(
            services_count=5))
        self.assertTrue(check.ok)
        self.assertEqual(check.approved, 5)
        self.assertEqual(check.completed, 0)

    def test_unstated_count_is_checked_against_the_standard_five(self):
        self.route_case([payer(approved=str(DEFAULT_SESSIONS))])
        check = verify_payer(self.make_client(), self.make_item())
        self.assertTrue(check.ok)

    def test_used_sessions_are_reported(self):
        """Nookal maintains Sessions_Completed, so usage can be read rather
        than derived from counting appointments."""
        self.route_case([payer(approved="5", completed="3")])
        check = verify_payer(self.make_client(), self.make_item(
            services_count=5))
        self.assertEqual(check.completed, 3)
        self.assertIn("3 used", check.message)

    # ------------------------------------------------------------ failing

    def test_zero_sessions_is_caught_as_unlimited(self):
        """The whole reason this check exists."""
        self.route_case([payer(approved="0")])
        check = verify_payer(self.make_client(), self.make_item(
            services_count=5))
        self.assertFalse(check.ok)
        self.assertIn("UNLIMITED", check.message)
        self.assertIn("set it to 5", check.message)

    def test_wrong_count_is_caught(self):
        self.route_case([payer(approved="15")])
        check = verify_payer(self.make_client(), self.make_item(
            services_count=5))
        self.assertFalse(check.ok)
        self.assertIn("15", check.message)
        self.assertIn("expects 5", check.message)

    def test_no_payer_yet_says_so(self):
        self.route_case([])
        check = verify_payer(self.make_client(), self.make_item())
        self.assertFalse(check.ok)
        self.assertIn("no payer on case 3466", check.message)

    def test_payer_on_a_different_case_does_not_count(self):
        """A payer on another case must not be mistaken for this one's."""
        self.fake.route("getCases", success({"cases": [
            {"ID": "3466", "payers": []},
            {"ID": "3469", "payers": [payer(approved="5")]},
        ]}))
        check = verify_payer(self.make_client(), self.make_item(
            services_count=5))
        self.assertFalse(check.ok)
        self.assertIn("no payer", check.message)

    def test_unreadable_count_is_flagged_not_assumed_good(self):
        self.route_case([payer(approved="")])
        check = verify_payer(self.make_client(), self.make_item())
        self.assertFalse(check.ok)
        self.assertIn("check it by hand", check.message)

    def test_api_failure_does_not_pass_the_check(self):
        self.fake.route("getCases", failure("service unavailable"))
        check = verify_payer(self.make_client(), self.make_item())
        self.assertFalse(check.ok)
        self.assertIn("could not read the case back", check.message)

    def test_item_without_a_case_cannot_be_verified(self):
        item = self.make_item()
        item.nookal = {}
        check = verify_payer(self.make_client(), item)
        self.assertFalse(check.ok)
        self.assertIn("no Nookal case recorded", check.message)


class MarkDoneTests(ClientTestCase):
    """The 'Payer added — mark done' button goes through verification."""

    def setUp(self):
        super().setUp()
        import threading

        import review_server as rs
        from referral_pipeline import Queue

        self.queue = Queue(os.path.join(self.tmp.name, "queue"))
        pdf = os.path.join(self.tmp.name, "r.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.1\n%%EOF\n")
        self.queue.save(ReviewItem(
            id="abc", pdf_path=pdf, state=NEEDS_PAYER,
            received="2026-08-04T09:00:00",
            fields={"patient_name": "Aiden Ward", "dob": "1960-05-04",
                    "services_count": 5},
            nookal={"patient_id": 2521, "case_id": 3466}))
        server = rs.ReviewServer(("127.0.0.1", 0), rs.Handler)
        server.queue = self.queue
        server.client = self.make_client()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def mark_done(self):
        import urllib.request
        req = urllib.request.Request(self.base + "/r/abc/done", data=b"",
                                     method="POST")
        with urllib.request.urlopen(req) as r:
            return r.read()

    def test_a_correct_payer_marks_done(self):
        self.fake.route("getCases", success({"cases": [
            {"ID": "3466", "payers": [payer(approved="5")]}]}))
        self.mark_done()
        self.assertEqual(self.queue.get("abc").state, DONE)

    def test_zero_sessions_keeps_it_open(self):
        """Saying 'done' must not close a referral whose payer is wrong."""
        self.fake.route("getCases", success({"cases": [
            {"ID": "3466", "payers": [payer(approved="0")]}]}))
        self.mark_done()
        item = self.queue.get("abc")
        self.assertEqual(item.state, NEEDS_PAYER)
        self.assertIn("UNLIMITED", item.message)
        self.assertIs(item.nookal["payer_verified"], False)

    def test_forgetting_to_add_the_payer_keeps_it_open(self):
        self.fake.route("getCases", success({"cases": [
            {"ID": "3466", "payers": []}]}))
        self.mark_done()
        self.assertEqual(self.queue.get("abc").state, NEEDS_PAYER)


if __name__ == "__main__":
    unittest.main()
