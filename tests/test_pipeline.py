"""The write sequence into Nookal, and the queue that feeds it.

This is the code that touches live patient records, so the tests lean on
what it must refuse to do.
"""

from __future__ import annotations

import os
import unittest

from referral_pipeline import (BLOCKED, DONE, NEEDS_PAYER, NEEDS_REVIEW,
                               Queue, ReviewItem, create_in_nookal,
                               payer_instructions, split_name)

from .base import ClientTestCase
from .fake_nookal import failure, success


def make_item(tmpdir, **fields) -> ReviewItem:
    pdf = os.path.join(tmpdir, "referral.pdf")
    with open(pdf, "wb") as f:
        f.write(b"%PDF-1.1\n%%EOF\n")
    base = {"patient_name": "Aiden Ward", "dob": "1960-05-04"}
    base.update(fields)
    return ReviewItem(id="abc123", pdf_path=pdf, received="2026-08-03T09:00:00",
                      fields={k: v for k, v in base.items() if v is not None})


class SplitNameTests(unittest.TestCase):
    def test_two_parts(self):
        self.assertEqual(split_name("Aiden Ward"), ("Aiden", "Ward"))

    def test_middle_names_stay_with_the_given_names(self):
        self.assertEqual(split_name("Linda May Kolb"), ("Linda May", "Kolb"))

    def test_ocr_capitalised_surname(self):
        self.assertEqual(split_name("Jill O'MALLEY"), ("Jill", "O'MALLEY"))

    def test_single_word_is_treated_as_a_surname(self):
        self.assertEqual(split_name("Ward"), ("", "Ward"))

    def test_blank(self):
        self.assertEqual(split_name("   "), ("", ""))


class QueueTests(ClientTestCase):
    def test_round_trip(self):
        q = Queue(os.path.join(self.tmp.name, "queue"))
        item = make_item(self.tmp.name)
        q.save(item)
        back = q.get("abc123")
        self.assertEqual(back.fields["patient_name"], "Aiden Ward")
        self.assertEqual(back.state, NEEDS_REVIEW)

    def test_missing_item_is_none_not_an_error(self):
        q = Queue(os.path.join(self.tmp.name, "queue"))
        self.assertIsNone(q.get("nope"))

    def test_listing_puts_review_work_first(self):
        q = Queue(os.path.join(self.tmp.name, "queue"))
        for n, state in (("a", DONE), ("b", NEEDS_REVIEW), ("c", NEEDS_PAYER)):
            item = make_item(self.tmp.name)
            item.id, item.state = n, state
            q.save(item)
        self.assertEqual([i.state for i in q.all()],
                         [NEEDS_REVIEW, NEEDS_PAYER, DONE])

    def test_flagged_lists_everything_not_check_digit_validated(self):
        item = make_item(self.tmp.name)
        item.confidence = {"medicare_no": "ok", "dob": "check",
                           "services_count": "missing"}
        self.assertEqual(sorted(item.flagged()), ["dob", "services_count"])


class CreateTests(ClientTestCase):
    def routes(self, exact=(), fuzzy=()):
        def search(params):
            if params.get("fuzzy_search"):
                return success({"patients": list(fuzzy)})
            return success({"patients": list(exact)})

        self.fake.routes_from({
            "searchPatients": search,
            "addPatient": success({"patient": {"patient_id": 2521}}),
            "updatePatientMedicareDetails": success({"ok": True}),
            "addCase": success({"cases": [{"ID": "3466"}]}),
            "uploadFile": lambda p: success(
                {"url": self.fake.s3_url(), "file_id": "file_abc"}),
            "setFileActive": success({"file_id": "file_abc"}),
        })

    # ------------------------------------------------------ refusing to act

    def test_missing_dob_blocks_before_any_call(self):
        self.routes()
        item = make_item(self.tmp.name, dob=None)
        result = create_in_nookal(self.make_client(), item)
        self.assertFalse(result.ok)
        self.assertEqual(result.state, BLOCKED)
        self.assertIn("dob", result.message)
        self.assertEqual(self.fake.sequence(), [])

    def test_ambiguous_match_creates_nothing(self):
        self.routes(exact=[{"ID": 1}, {"ID": 2}])
        result = create_in_nookal(self.make_client(),
                                  make_item(self.tmp.name))
        self.assertEqual(result.state, BLOCKED)
        self.assertIn("several patients match", result.message)
        self.assertEqual(self.fake.count("addPatient"), 0)
        self.assertEqual(self.fake.count("addCase"), 0)

    def test_fuzzy_near_miss_creates_nothing(self):
        self.routes(exact=[], fuzzy=[{"ID": 9}])
        result = create_in_nookal(self.make_client(),
                                  make_item(self.tmp.name))
        self.assertEqual(result.state, BLOCKED)
        self.assertIn("duplicate", result.message)
        self.assertEqual(self.fake.count("addPatient"), 0)

    def test_invalid_medicare_is_skipped_not_written(self):
        self.routes(exact=[{"patient_id": 2521}])
        item = make_item(self.tmp.name, medicare_no="4070953273")
        result = create_in_nookal(self.make_client(), item)
        self.assertTrue(result.ok)
        self.assertEqual(self.fake.count("updatePatientMedicareDetails"), 0)
        self.assertIn("fails check digit", result.message)

    # ------------------------------------------------------- the happy path

    def test_full_sequence(self):
        self.routes(exact=[{"patient_id": 2521}])
        item = make_item(self.tmp.name, medicare_no="4070953263",
                         referral_date="2025-09-24", services_count=5)
        result = create_in_nookal(self.make_client(), item)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.state, NEEDS_PAYER)
        self.assertEqual(result.patient_id, 2521)
        self.assertEqual(result.case_id, 3466)
        self.assertEqual(
            [e for e in self.fake.endpoints() if e != "searchPatients"],
            ["updatePatientMedicareDetails", "addCase", "uploadFile",
             "/s3/upload", "setFileActive"])

    def test_case_is_always_titled_GP_CCMP(self):
        self.routes(exact=[{"patient_id": 2521}])
        create_in_nookal(self.make_client(), make_item(self.tmp.name))
        self.assertEqual(self.fake.last_params("addCase")["title"], "GP CCMP")

    def test_pdf_is_attached_to_the_case_it_created(self):
        self.routes(exact=[{"patient_id": 2521}])
        create_in_nookal(self.make_client(), make_item(self.tmp.name))
        self.assertEqual(self.fake.last_params("uploadFile")["case_id"],
                         "3466")

    def test_no_payer_is_ever_created(self):
        """The session cap lives in the payer wizard, where 0 means
        Unlimited. Nothing here may touch it."""
        self.routes(exact=[{"patient_id": 2521}])
        create_in_nookal(self.make_client(), make_item(self.tmp.name))
        self.assertEqual(self.fake.count("editCasePayer"), 0)

    def test_upload_failure_does_not_lose_the_case(self):
        self.routes(exact=[{"patient_id": 2521}])
        self.fake.route("uploadFile", failure("storage unavailable"))
        result = create_in_nookal(self.make_client(), make_item(self.tmp.name))
        self.assertTrue(result.ok)
        self.assertEqual(result.case_id, 3466)
        self.assertIn("PDF NOT attached", result.message)

    def test_case_failure_blocks_and_reports(self):
        self.routes(exact=[{"patient_id": 2521}])
        self.fake.route("addCase", failure("something went wrong"))
        result = create_in_nookal(self.make_client(), make_item(self.tmp.name))
        self.assertFalse(result.ok)
        self.assertEqual(result.state, BLOCKED)
        self.assertIn("case creation failed", result.message)

    def test_dry_run_writes_nothing(self):
        self.routes(exact=[{"patient_id": 2521}])
        client = self.make_client(dry_run=True)
        create_in_nookal(client, make_item(self.tmp.name))
        for endpoint in ("addPatient", "addCase", "uploadFile",
                         "updatePatientMedicareDetails"):
            self.assertEqual(self.fake.count(endpoint), 0)

    def test_dry_run_completes_the_whole_flow_for_a_new_patient(self):
        """A rehearsal has to reach the payer hand-off, or it cannot be used
        to show anyone the process. Before this, a dry run stopped at
        "patient created but no id came back"."""
        self.routes(exact=[], fuzzy=[])          # nobody matches: creates
        result = create_in_nookal(self.make_client(dry_run=True),
                                  make_item(self.tmp.name))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.state, NEEDS_PAYER)
        self.assertIn("DRY RUN", result.message)
        self.assertIn("case created", result.message)
        self.assertEqual(self.fake.count("addPatient"), 0)

    def test_dry_run_does_not_print_placeholder_ids_as_if_real(self):
        self.routes(exact=[], fuzzy=[])
        result = create_in_nookal(self.make_client(dry_run=True),
                                  make_item(self.tmp.name))
        self.assertNotIn("(0)", result.message)

    # -------------------------------------------------------- queue effects

    def test_success_moves_the_item_to_needs_payer(self):
        self.routes(exact=[{"patient_id": 2521}])
        q = Queue(os.path.join(self.tmp.name, "queue"))
        item = make_item(self.tmp.name)
        q.save(item)
        create_in_nookal(self.make_client(), item, queue=q)
        self.assertEqual(q.get("abc123").state, NEEDS_PAYER)
        self.assertEqual(q.get("abc123").nookal["case_id"], 3466)


class PayerInstructionTests(ClientTestCase):
    def test_states_the_session_count_when_known(self):
        item = make_item(self.tmp.name, services_count=5)
        instructions = payer_instructions(item)
        self.assertEqual(instructions["sessions"], 5)
        self.assertTrue(instructions["sessions_stated"])
        self.assertIn("Never enter 0", instructions["sessions_warning"])

    def test_unstated_count_falls_back_to_the_standard_five(self):
        """Clinic policy: the patient tracks their own remaining
        entitlement, so a silent referral just gets the standard 5."""
        instructions = payer_instructions(make_item(self.tmp.name))
        self.assertEqual(instructions["sessions"], 5)
        self.assertFalse(instructions["sessions_stated"])
        self.assertIn("Not stated", instructions["sessions_warning"])

    def test_zero_is_never_presented_as_acceptable(self):
        """0 does not mean "no sessions" in Nookal, it means Unlimited —
        the one value that must never be entered, stated or not."""
        for item in (make_item(self.tmp.name),
                     make_item(self.tmp.name, services_count=3)):
            instructions = payer_instructions(item)
            self.assertNotEqual(instructions["sessions"], 0)
            self.assertIn("Never enter 0", instructions["sessions_warning"])
            self.assertIn("Unlimited", instructions["sessions_warning"])

    def test_carries_the_gp_details_across(self):
        item = make_item(self.tmp.name, gp_name="Dr Kym R. Horsnell",
                         gp_provider_number="040501AW")
        instructions = payer_instructions(item)
        self.assertEqual(instructions["provider_number"], "040501AW")
        self.assertEqual(instructions["referring_gp"], "Dr Kym R. Horsnell")


if __name__ == "__main__":
    unittest.main()
