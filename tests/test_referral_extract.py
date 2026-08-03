"""Field extraction, tested on text fixtures taken from the real referrals.

No PDFs and no OCR here — those need pdfplumber/pypdf/tesseract. These
cover the pattern matching and validation, which is where the mistakes that
would reach a patient record get made.
"""

from __future__ import annotations

import unittest

from referral_extract import (CHECK, MISSING, OK, extract_fields,
                              find_medicare, find_provider, find_referral_date,
                              find_sessions, to_iso, trim_name)

# Shortened from A. Ward.pdf — narrative letter, provider number labelled.
LETTER = """RE: Mr Aiden Ward (DOB: 04/05/1960)
Old Northern Road Medical Centre Phone: (07) 3353 2422
Dr Kym R. Horsnell
Provider number:040501AW
24 September 2025
Thank you for seeing Mr Aiden Ward aged 65 years.
I have recommended (5) sessions via GPCCMP to assist with this.
"""

# Shortened from a Best Practice export — structured plan, labelled Medicare.
PLAN = """RE: Mrs Linda May Kolb
DOB: 04/12/1953
16/07/2025
5 visits
Dr Katya Groeneveld
420427AA
Name: Mrs Linda May Kolb Medicare No: 4070953263
"""


class DateTests(unittest.TestCase):
    def test_australian_day_first_order(self):
        self.assertEqual(to_iso("04/05/1960"), "1960-05-04")

    def test_long_form(self):
        self.assertEqual(to_iso("24 September 2025"), "2025-09-24")

    def test_unparseable(self):
        self.assertIsNone(to_iso("not a date"))

    def test_letter_date_is_not_the_dob(self):
        field = find_referral_date(LETTER, "1960-05-04")
        self.assertEqual(field.value, "2025-09-24")

    def test_earliest_date_wins_over_a_later_long_form_one(self):
        """A boilerplate footnote mentioning "1 July 2025" must not beat the
        real letter date — this happened on a real referral."""
        text = ("15/09/2025\nDear Sir/Madam,\n"
                "NB: From 1 July 2025, with the recent MBS changes...")
        self.assertEqual(find_referral_date(text, None).value, "2025-09-15")

    def test_implausible_date_is_rejected_not_reported(self):
        """OCR produced 1951 for a 2025 letter. A wrong date silently
        written is worse than a missing one the reviewer fills in."""
        field = find_referral_date("05/12/1951\nDear Sir", None)
        self.assertEqual(field.confidence, MISSING)
        self.assertIn("implausible", field.note)

    def test_future_date_is_rejected(self):
        self.assertEqual(find_referral_date("01/01/2099", None).confidence,
                         MISSING)


class MedicareTests(unittest.TestCase):
    def test_labelled_number(self):
        field = find_medicare(PLAN)
        self.assertEqual(field.value, "4070953263")
        self.assertEqual(field.confidence, OK)

    def test_unlabelled_number_found_by_check_digit(self):
        """One referral prints the Medicare number bare in the address
        block, under the phone number and with no label at all."""
        text = "Miss Jasmine Jin\n0466069349\n2775792963\nDOB: 25/07/1997"
        field = find_medicare(text)
        self.assertEqual(field.value, "2775792963")
        self.assertEqual(field.confidence, OK)

    def test_phone_numbers_are_never_mistaken_for_medicare(self):
        """Australian phone numbers start with 0; Medicare numbers start
        2-6. The leading-digit rule keeps them apart."""
        text = "Phone: 0733554082 Mob: 0412090276 Fax: 0733544042"
        self.assertEqual(find_medicare(text).confidence, MISSING)

    def test_labelled_but_invalid_number_is_flagged_not_dropped(self):
        field = find_medicare("Medicare No: 4070953273")
        self.assertEqual(field.confidence, CHECK)
        self.assertIn("FAILS check digit", field.note)

    def test_absent_number_says_where_to_get_it(self):
        field = find_medicare(LETTER)
        self.assertEqual(field.confidence, MISSING)
        self.assertIn("card", field.note)


class ProviderTests(unittest.TestCase):
    def test_labelled_provider_number(self):
        field = find_provider(LETTER)
        self.assertEqual(field.value, "040501AW")
        self.assertEqual(field.confidence, OK)

    def test_bare_provider_number_on_its_own_line(self):
        self.assertEqual(find_provider(PLAN).value, "420427AA")

    def test_high_practice_location_value(self):
        """These come from real referrals and were rejected until the
        practice-location alphabet was widened past F."""
        for number in ("062626LW", "480353JB"):
            with self.subTest(number=number):
                self.assertEqual(find_provider(f"Dr X\n{number}\n").value,
                                 number)

    def test_invalid_candidates_are_ignored(self):
        self.assertEqual(find_provider("ABN: 41 107 480 400\n12345XY").
                         confidence, MISSING)


class SessionTests(unittest.TestCase):
    def test_parenthesised_count(self):
        self.assertEqual(find_sessions(LETTER).value, 5)

    def test_visits_wording(self):
        self.assertEqual(find_sessions(PLAN).value, 5)

    def test_single_session(self):
        text = "I request she have 1 session under her GPCCMP."
        self.assertEqual(find_sessions(text).value, 1)

    def test_never_reported_as_certain(self):
        """This number sets the session cap, and Sessions=0 in Nookal means
        Unlimited — it must always reach a human."""
        for text in (LETTER, PLAN, "5 sessions"):
            self.assertEqual(find_sessions(text).confidence, CHECK)

    def test_vague_wording_yields_nothing(self):
        text = "She thinks she has a couple of visits left this year."
        self.assertEqual(find_sessions(text).confidence, MISSING)

    def test_a_visit_date_is_not_a_session_count(self):
        self.assertEqual(find_sessions("Visit date:06/10/2025").confidence,
                         MISSING)

    def test_implausible_counts_ignored(self):
        self.assertEqual(find_sessions("40 visits").confidence, MISSING)


class NameTrimTests(unittest.TestCase):
    def test_stops_at_letterhead_words(self):
        self.assertEqual(trim_name("Jamie Sutherland KEPERRA Keperra QLD"),
                         "Jamie Sutherland")

    def test_stops_at_dob(self):
        self.assertEqual(trim_name("Malcolm Vice DOB"), "Malcolm Vice")

    def test_leaves_a_clean_name_alone(self):
        self.assertEqual(trim_name("Kym R. Horsnell"), "Kym R. Horsnell")


class WholeReferralTests(unittest.TestCase):
    def test_letter_fields(self):
        fields = extract_fields(LETTER)
        self.assertEqual(fields["patient_name"].value, "Aiden Ward")
        self.assertEqual(fields["dob"].value, "1960-05-04")
        self.assertEqual(fields["gp_name"].value, "Dr Kym R. Horsnell")
        self.assertEqual(fields["gp_provider_number"].value, "040501AW")
        self.assertEqual(fields["services_count"].value, 5)

    def test_plan_fields(self):
        fields = extract_fields(PLAN)
        self.assertEqual(fields["patient_name"].value, "Linda May Kolb")
        self.assertEqual(fields["medicare_no"].value, "4070953263")
        self.assertEqual(fields["dob"].value, "1953-12-04")

    def test_nothing_is_reported_as_ok_except_validated_numbers(self):
        """Only the two check-digit fields can be trusted without a human.
        Everything else is a heuristic and must be marked for review."""
        for text in (LETTER, PLAN):
            for name, field in extract_fields(text).items():
                if field.confidence == OK:
                    self.assertIn(name, ("medicare_no",
                                         "gp_provider_number"))


if __name__ == "__main__":
    unittest.main()
