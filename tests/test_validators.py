"""Pure-logic tests: no network, no config, no client instance."""

from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nookal_client import (NookalClient, format_case_notes,  # noqa: E402
                           medicare_number_valid, plausible_dob,
                           provider_number_valid)


class MedicareNumberTests(unittest.TestCase):
    # From the three sample referrals the algorithm was derived against.
    SAMPLES = ["4081334278", "2123456701"]

    def test_sample_referral_numbers_validate(self):
        for number in self.SAMPLES:
            with self.subTest(number=number):
                self.assertTrue(medicare_number_valid(number))

    def test_formatting_is_ignored(self):
        self.assertTrue(medicare_number_valid("4081 33427 8"))
        self.assertTrue(medicare_number_valid("4081-33427-8"))

    def test_wrong_check_digit_rejected(self):
        # Same number with digit 9 bumped by one.
        self.assertFalse(medicare_number_valid("4081334268"))

    def test_first_digit_must_be_2_to_6(self):
        # Digits 1-8 sum to a valid check digit, but the leading digit is
        # outside the issuable range.
        self.assertFalse(medicare_number_valid("1081334278"))
        self.assertFalse(medicare_number_valid("7081334278"))

    def test_length_must_be_ten(self):
        self.assertFalse(medicare_number_valid("408133427"))
        self.assertFalse(medicare_number_valid("40813342788"))

    def test_non_numeric_and_empty_rejected(self):
        for bad in ("", "not a number", "ABCDEFGHIJ", None):
            with self.subTest(value=bad):
                self.assertFalse(medicare_number_valid(bad))

    def test_card_issue_digit_is_not_checked(self):
        # Digit 10 is the card issue number; changing it must not matter.
        base = "408133427"
        self.assertTrue(all(medicare_number_valid(base + str(d))
                            for d in range(10)))


class ProviderNumberTests(unittest.TestCase):
    # Every provider number appearing in the ten sample referrals.
    REAL = ["0138434F", "228981BX", "040501AW", "420427AA", "4603231X",
            "571684EA", "057287BW", "4334685H", "062626LW", "480353JB"]

    def test_sample_referral_numbers_validate(self):
        for number in self.REAL:
            with self.subTest(number=number):
                self.assertTrue(provider_number_valid(number))

    def test_practice_location_value_above_F_is_accepted(self):
        """Practice-location values run 0-9 then A-Y (no I or O). Capping
        the alphabet at F silently rejected two valid numbers out of ten
        real referrals — every practice with a location value above 15."""
        for number in ("062626LW", "480353JB"):
            with self.subTest(number=number):
                self.assertTrue(provider_number_valid(number))

    def test_ambiguous_letters_are_not_valid_locations(self):
        # I and O are excluded so they cannot be read as 1 and 0.
        self.assertFalse(provider_number_valid("062626IW"))
        self.assertFalse(provider_number_valid("062626OW"))

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(provider_number_valid(" 0138434f "))
        self.assertTrue(provider_number_valid("228981bx"))

    def test_short_stem_is_left_padded(self):
        # 0138434F with the leading zero dropped, as some sources print it.
        self.assertTrue(provider_number_valid("138434F"))

    def test_wrong_check_character_rejected(self):
        self.assertFalse(provider_number_valid("0138434A"))

    def test_wrong_practice_location_rejected(self):
        self.assertFalse(provider_number_valid("0138433F"))

    def test_malformed_rejected(self):
        for bad in ("", "AB", "12", "ABCDEFGH", "01384345"):
            with self.subTest(value=bad):
                self.assertFalse(provider_number_valid(bad))

    def test_non_digit_stem_rejected(self):
        self.assertFalse(provider_number_valid("01X8434F"))


class DobTests(unittest.TestCase):
    def test_plausible(self):
        self.assertTrue(plausible_dob("1990-01-01"))

    def test_future_rejected(self):
        future = dt.date.today() + dt.timedelta(days=1)
        self.assertFalse(plausible_dob(future.isoformat()))

    def test_today_accepted(self):
        self.assertTrue(plausible_dob(dt.date.today().isoformat()))

    def test_unparseable_rejected(self):
        for bad in ("18/06/2025", "1990-13-01", "", None, 19900101):
            with self.subTest(value=bad):
                self.assertFalse(plausible_dob(bad))

    def test_age_bounds_enforced(self):
        old = dt.date.today().replace(year=dt.date.today().year - 130)
        self.assertFalse(plausible_dob(old.isoformat()))
        self.assertFalse(plausible_dob("1990-01-01", min_age=50))


class ExtractTests(unittest.TestCase):
    def test_finds_key_at_depth(self):
        body = {"status": "success", "data": {"results": {"cases": [1, 2]}}}
        self.assertEqual(NookalClient.extract(body, "cases"), [1, 2])

    def test_priority_beats_depth(self):
        """A specific key deeper in the tree still beats a shallow fallback.

        This is the property the reference-data helpers rely on: extract(
        body, "cases", "results") must never return the generic "results"
        wrapper just because it sits closer to the root.
        """
        body = {"results": "GENERIC", "data": {"deep": {"cases": "SPECIFIC"}}}
        self.assertEqual(NookalClient.extract(body, "cases", "results"),
                         "SPECIFIC")

    def test_searches_through_lists(self):
        body = {"data": [{"a": 1}, {"patients": [{"ID": 9}]}]}
        self.assertEqual(NookalClient.extract(body, "patients"), [{"ID": 9}])

    def test_missing_key_returns_none(self):
        self.assertIsNone(NookalClient.extract({"a": 1}, "cases"))

    def test_non_dict_input_is_safe(self):
        self.assertIsNone(NookalClient.extract(None, "cases"))
        self.assertIsNone(NookalClient.extract("text", "cases"))


class CaseNotesTests(unittest.TestCase):
    FULL = {
        "received_date": "2026-08-01",
        "referral_date": "2026-07-20",
        "gp_name": "Dr A Smith",
        "gp_provider_number": "0138434F",
        "gp_practice": "Northside Medical",
        "services_count": 5,
        "discipline": "Physiotherapy",
        "item_number": 10960,
        "medicare_no": "4081334278",
        "conditions": ["Osteoarthritis", "Type 2 diabetes"],
        "form_type": "CCMP",
    }

    def test_all_fields_render(self):
        notes = format_case_notes(self.FULL)
        for expected in ("2026-08-01", "Dr A Smith", "0138434F",
                         "Northside Medical", "Physiotherapy", "10960",
                         "4081334278", "Osteoarthritis, Type 2 diabetes",
                         "CCMP"):
            self.assertIn(expected, notes)

    def test_empty_referral_does_not_raise(self):
        notes = format_case_notes({})
        self.assertIn("unknown", notes)
        self.assertIn("entered by referral automation", notes)

    def test_manual_step_footer_always_present(self):
        # The footer is what tells the operator the payer/sessions still
        # need doing by hand — it must survive every input shape.
        for referral in ({}, self.FULL, {"gp_name": "Dr B"}):
            self.assertIn("set manually in Add Payer",
                          format_case_notes(referral))

    def test_flag_marks_services_for_verification(self):
        flagged = dict(self.FULL, services_flagged=True)
        self.assertIn("[VERIFY]", format_case_notes(flagged))
        self.assertNotIn("[VERIFY]", format_case_notes(self.FULL))

    def test_conditions_accepts_string_or_list(self):
        as_string = format_case_notes(dict(self.FULL, conditions="Asthma"))
        self.assertIn("Conditions: Asthma", as_string)

    def test_missing_optionals_are_omitted_not_blank(self):
        notes = format_case_notes({"gp_name": "Dr C"})
        self.assertNotIn("Practice:", notes)
        self.assertNotIn("Medicare:", notes)
        self.assertNotIn("Conditions:", notes)


if __name__ == "__main__":
    unittest.main()
