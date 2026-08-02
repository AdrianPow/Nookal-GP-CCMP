"""The safety rails around patient creation, case titles and Medicare writes.

These are the behaviours that protect live clinic data, so they are tested
for what the client *refuses* to do as much as what it does.
"""

from __future__ import annotations

import unittest

from nookal_client import NookalError

from .base import ClientTestCase
from .fake_nookal import success


class SearchOrCreateTests(ClientTestCase):
    """A duplicate patient record is worse than a pause for review, so a
    new record may only be created when exact AND fuzzy both come back
    empty."""

    def route_search(self, exact, fuzzy):
        def handler(params):
            if params.get("fuzzy_search"):
                return success({"patients": fuzzy})
            return success({"patients": exact})

        self.fake.route("searchPatients", handler)

    def test_single_exact_match_is_used(self):
        self.route_search(exact=[{"ID": 7}], fuzzy=[])
        client = self.make_client()
        patient, disposition = client.search_or_create_patient(
            "Jane", "Doe", "1990-01-01")
        self.assertEqual(disposition, "matched")
        self.assertEqual(patient, {"ID": 7})
        self.assertEqual(self.fake.count("addPatient"), 0)

    def test_several_exact_matches_are_ambiguous(self):
        self.route_search(exact=[{"ID": 7}, {"ID": 8}], fuzzy=[])
        client = self.make_client()
        patient, disposition = client.search_or_create_patient(
            "Jane", "Doe", "1990-01-01")
        self.assertEqual(disposition, "ambiguous")
        self.assertIsNone(patient)
        self.assertEqual(self.fake.count("addPatient"), 0)

    def test_fuzzy_near_miss_stops_before_creating(self):
        self.route_search(exact=[], fuzzy=[{"ID": 9, "last_name": "Doe"}])
        client = self.make_client()
        patient, disposition = client.search_or_create_patient(
            "Jane", "Doe", "1990-01-01")
        self.assertEqual(disposition, "fuzzy_review")
        self.assertIsNone(patient)
        self.assertEqual(self.fake.count("addPatient"), 0)

    def test_creates_only_when_both_searches_are_empty(self):
        self.route_search(exact=[], fuzzy=[])
        self.fake.route("addPatient", success({"patient": {"ID": 42}}))
        client = self.make_client()
        patient, disposition = client.search_or_create_patient(
            "Jane", "Doe", "1990-01-01", create_fields={"mobile": "0400000001"})
        self.assertEqual(disposition, "created")
        self.assertEqual(patient, {"ID": 42})
        params = self.fake.last_params("addPatient")
        self.assertEqual(params["first_name"], "Jane")
        self.assertEqual(params["date_of_birth"], "1990-01-01")
        self.assertEqual(params["mobile"], "0400000001")

    def test_created_patient_unwrapped_from_a_list(self):
        self.route_search(exact=[], fuzzy=[])
        self.fake.route("addPatient", success({"patients": [{"ID": 43}]}))
        client = self.make_client()
        patient, disposition = client.search_or_create_patient(
            "Jane", "Doe", "1990-01-01")
        self.assertEqual(disposition, "created")
        self.assertEqual(patient, {"ID": 43})

    def test_exact_search_sends_all_three_identifiers(self):
        self.route_search(exact=[{"ID": 7}], fuzzy=[])
        client = self.make_client()
        client.search_or_create_patient("Jane", "Doe", "1990-01-01")
        params = self.fake.calls_to("searchPatients")[0].params
        self.assertEqual(params["first_name"], "Jane")
        self.assertEqual(params["last_name"], "Doe")
        self.assertEqual(params["date_of_birth"], "1990-01-01")


class CaseTitleTests(ClientTestCase):
    """Title is a clinic-wide managed dropdown: a novel string risks
    polluting it for every case in the practice."""

    def setUp(self):
        super().setUp()
        self.fake.route("addCase", success({"case": {"ID": 100}}))

    def test_default_title_is_the_locked_value(self):
        client = self.make_client()
        client.add_case(5)
        self.assertEqual(self.fake.last_params("addCase")["title"], "GP CCMP")

    def test_novel_title_is_refused_before_any_request(self):
        client = self.make_client()
        with self.assertRaises(NookalError) as ctx:
            client.add_case(5, title="Physio Plan")
        self.assertIn("pollute", str(ctx.exception))
        self.assertEqual(self.fake.count("addCase"), 0)

    def test_explicit_default_title_is_allowed(self):
        client = self.make_client()
        client.add_case(5, title="GP CCMP")
        self.assertEqual(self.fake.count("addCase"), 1)

    def test_title_check_is_exact_not_fuzzy(self):
        client = self.make_client()
        for near_miss in ("gp ccmp", "GP CCMP ", "GPCCMP"):
            with self.subTest(title=near_miss):
                with self.assertRaises(NookalError):
                    client.add_case(5, title=near_miss)
        self.assertEqual(self.fake.count("addCase"), 0)

    def test_allow_any_title_opens_the_gate(self):
        client = self.make_client()
        client.allow_any_title()
        client.add_case(5, title="ZZ DIAGNOSTIC TITLE TEST")
        self.assertEqual(self.fake.last_params("addCase")["title"],
                         "ZZ DIAGNOSTIC TITLE TEST")

    def test_the_override_does_not_leak_between_clients(self):
        opened = self.make_client()
        opened.allow_any_title()
        fresh = self.make_client()
        with self.assertRaises(NookalError):
            fresh.add_case(5, title="Physio Plan")

    def test_referrer_is_not_sent_by_default(self):
        """Case 'Referrer' is the marketing-source field, not the referring
        doctor — sending a GP id there would silently mislabel the case."""
        client = self.make_client()
        client.add_case(5)
        self.assertNotIn("referrer_id", self.fake.last_params("addCase"))


class MedicareWriteTests(ClientTestCase):
    def test_invalid_number_is_refused_before_any_request(self):
        client = self.make_client()
        with self.assertRaises(NookalError) as ctx:
            client.update_medicare(5, "4081334268", "1")
        self.assertIn("check-digit", str(ctx.exception))
        self.assertEqual(self.fake.count("updateMedicareDetails"), 0)

    def test_valid_number_is_written_with_irn_and_expiry(self):
        self.fake.route("updateMedicareDetails", success({"ok": True}))
        client = self.make_client()
        client.update_medicare(5, "4081334278", "1", "2027-11-30")
        params = self.fake.last_params("updateMedicareDetails")
        self.assertEqual(params["medicare_no"], "4081334278")
        self.assertEqual(params["medicare_irn"], "1")
        self.assertEqual(params["expiry_date"], "2027-11-30")

    def test_expiry_is_optional(self):
        self.fake.route("updateMedicareDetails", success({"ok": True}))
        client = self.make_client()
        client.update_medicare(5, "4081334278", "1")
        self.assertNotIn("expiry_date",
                         self.fake.last_params("updateMedicareDetails"))

    def test_refusal_applies_in_dry_run_too(self):
        """dry_run must not become a way to smuggle a bad number past the
        validator — the guard runs before the dry-run branch."""
        client = self.make_client(dry_run=True)
        with self.assertRaises(NookalError):
            client.update_medicare(5, "0000000000", "1")


class AppointmentCountTests(ClientTestCase):
    def test_counts_completed_appointments_across_pages(self):
        def paged(params):
            page = int(params.get("page", 1))
            if page == 1:
                return success({"appointments": [{"ID": i}
                                                 for i in range(200)]})
            return success({"appointments": [{"ID": i} for i in range(5)]})

        self.fake.route("getAppointments", paged)
        client = self.make_client()
        total = client.count_completed_appointments(5, "2026-01-01")
        self.assertEqual(total, 205)
        self.assertEqual(self.fake.count("getAppointments"), 2)

    def test_single_short_page_stops_immediately(self):
        self.fake.route("getAppointments",
                        success({"appointments": [{"ID": 1}]}))
        client = self.make_client()
        self.assertEqual(client.count_completed_appointments(5, "2026-01-01"), 1)
        self.assertEqual(self.fake.count("getAppointments"), 1)

    def test_empty_result_is_zero_not_an_error(self):
        self.fake.route("getAppointments", success({"appointments": []}))
        client = self.make_client()
        self.assertEqual(client.count_completed_appointments(5, "2026-01-01"), 0)

    def test_status_filter_is_always_applied(self):
        self.fake.route("getAppointments", success({"appointments": []}))
        client = self.make_client()
        client.count_completed_appointments(5, "2026-01-01", service_id=12)
        params = self.fake.last_params("getAppointments")
        self.assertEqual(params["appt_status"], "Completed")
        self.assertEqual(params["service_id"], "12")
        self.assertEqual(params["date_from"], "2026-01-01")


if __name__ == "__main__":
    unittest.main()
