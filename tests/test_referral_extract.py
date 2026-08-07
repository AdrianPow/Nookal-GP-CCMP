"""Field extraction, tested on text fixtures taken from the real referrals.

No PDFs and no OCR here — those need pdfplumber/pypdf/tesseract. These
cover the pattern matching and validation, which is where the mistakes that
would reach a patient record get made.
"""

from __future__ import annotations

import unittest

from referral_extract import (CHECK, MISSING, OK, extract_fields,
                              find_medicare, find_provider, find_referral_date,
                              find_sessions, text_layer_is_thin, to_iso,
                              trim_name)

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


class TextLayerTests(unittest.TestCase):
    """Deciding whether a PDF's own text is worth using, or OCR should run.

    A scanned care plan carried a text layer of 143 characters — a garbled
    OCR of the letterhead alone ("Health hsurarrce Commission") — while
    every real field stayed locked in the page image. It was treated as
    digital, so OCR never ran and the referral came out completely empty.
    """

    def test_a_real_digital_referral_keeps_its_own_text(self):
        # Measured across the real corpus: 900-1500 characters a page.
        self.assertFalse(text_layer_is_thin(3745, 4))    # 936/page
        self.assertFalse(text_layer_is_thin(3095, 2))    # 1548/page
        self.assertFalse(text_layer_is_thin(8558, 8))    # 1070/page

    def test_a_letterhead_only_text_layer_is_thin(self):
        self.assertTrue(text_layer_is_thin(143, 1))

    def test_no_text_at_all_is_thin(self):
        self.assertTrue(text_layer_is_thin(0, 4))

    def test_zero_pages_does_not_divide_by_zero(self):
        self.assertTrue(text_layer_is_thin(0, 0))


class FormLayoutTests(unittest.TestCase):
    """Three referral layouts turned up in one clinic's post. Only the GP
    letter was handled at first; these are the other two."""

    def test_patients_name_label(self):
        text = "CHRONIC DISEASE MANAGEMENT\nPatient's Name: Miss Holly Kell\n"
        self.assertEqual(extract_fields(text)["patient_name"].value,
                         "Holly Kell")

    def test_curly_apostrophe_in_the_label(self):
        text = "Patient’s Name: Ms Cheryl Moss Date of Birth: 12/11/1960"
        self.assertEqual(extract_fields(text)["patient_name"].value,
                         "Cheryl Moss")

    def test_name_does_not_run_into_the_next_label(self):
        """The CDM forms put the next field's label on the same line, which
        produced "Cheryl Moss Date" until label words ended the capture."""
        text = "Patient's Name: Ms Cheryl Moss Date of Birth: 12/11/1960"
        fields = extract_fields(text)
        self.assertEqual(fields["patient_name"].value, "Cheryl Moss")
        self.assertEqual(fields["dob"].value, "1960-11-12")

    def test_epc_form_first_name_and_surname_boxes(self):
        text = ("Patient Details\nMedicare Number\n3349170497\n"
                "First Name\nCheryl Surname Moss\nAddress\n")
        self.assertEqual(extract_fields(text)["patient_name"].value,
                         "Cheryl Moss")

    def test_date_of_birth_spelled_out(self):
        text = "Patient's Name: Ms X Y\nDate of Birth: 12/11/1960"
        self.assertEqual(extract_fields(text)["dob"].value, "1960-11-12")

    def test_text_between_the_dob_label_and_the_date(self):
        """One form extracts as 'DOB: Patient Demographics.  12/11/1960'."""
        text = "Ms Cheryl Moss   DOB: Patient Demographics.  12/11/1960"
        self.assertEqual(extract_fields(text)["dob"].value, "1960-11-12")

    def test_the_letter_layout_still_works(self):
        self.assertEqual(extract_fields(LETTER)["patient_name"].value,
                         "Aiden Ward")


class MangledSeparatorTests(unittest.TestCase):
    """One practice's PDFs render '/' as '1', so every date in the document
    extracts as a run of ten digits."""

    def test_repairs_a_recoverable_date(self):
        field = extract_fields("Date of Birth: 1510812002")["dob"]
        self.assertEqual(field.value, "2002-08-15")

    def test_says_the_separators_were_unreadable(self):
        field = extract_fields("Date of Birth: 1510812002")["dob"]
        self.assertEqual(field.confidence, CHECK)
        self.assertIn("CONFIRM", field.note)
        self.assertIn("1510812002", field.note)

    def test_ten_digits_that_are_not_a_date_are_refused(self):
        """Only ever repaired when 1s sit exactly where separators belong —
        otherwise a wrong date of birth would be written silently."""
        field = extract_fields("Date of Birth: 4070953263")["dob"]
        self.assertEqual(field.confidence, MISSING)
        self.assertIn("could not be read as a date", field.note)

    def test_an_impossible_repair_is_refused(self):
        # 45/99/2002 is not a date even after repairing the separators.
        self.assertEqual(extract_fields("DOB: 4519912002")["dob"].confidence,
                         MISSING)


class NameTrimTests(unittest.TestCase):
    def test_stops_at_letterhead_words(self):
        self.assertEqual(trim_name("Jamie Sutherland KEPERRA Keperra QLD"),
                         "Jamie Sutherland")

    def test_stops_at_dob(self):
        self.assertEqual(trim_name("Malcolm Vice DOB"), "Malcolm Vice")

    def test_leaves_a_clean_name_alone(self):
        self.assertEqual(trim_name("Kym R. Horsnell"), "Kym R. Horsnell")


class EpcFormLayoutTests(unittest.TestCase):
    """The Department of Health EPC form, as pypdf extracts it. Where the
    line breaks land inside the First Name / Surname boxes depends on how
    wide the typed values are, and the form has no letterhead at all."""

    EPC = ("Enhanced Primary Care (EPC) Program \n"
           "Referral Form for Allied Health Services under Medicare \n"
           "To be completed by referring GP\n"
           "GP details\n"
           "Provider Number 2202997K NOTE: Relevant MBS item(s) above must be\n"
           "first referred allied health service for Medicare\n"
           "Name\nDr Maya Venkatesh\n"
           "Address\nBrendale Medical Centre\n249b Leitchs Road\n"
           "Patient Details\nMedicare Number 2535664148\n"
           "First Name Jane\nSurname \nBrooker\n"
           "Address\n30 Fairlane Street\n"
           "Allied Health Professional Embrace Movement Clinic\n"
           " Date Signed: 17/06/2025\n")

    def test_surname_on_its_own_line(self):
        """"First Name Jane\\nSurname \\nBrooker" — the value fits beside its
        label, so the break falls before the Surname box, not after it."""
        self.assertEqual(extract_fields(self.EPC)["patient_name"].value,
                         "Jane Brooker")

    def test_the_other_box_arrangement_still_works(self):
        text = "First Name\nCheryl Surname Moss\nAddress\n"
        self.assertEqual(extract_fields(text)["patient_name"].value,
                         "Cheryl Moss")

    def test_an_empty_first_name_box_is_not_read_as_a_name(self):
        text = "First Name\nSurname\nBrooker\n"
        self.assertIsNone(extract_fields(text)["patient_name"].value)

    def test_practice_comes_from_the_gp_address_block(self):
        self.assertEqual(extract_fields(self.EPC)["gp_practice"].value,
                         "Brendale Medical Centre")

    def test_the_forms_own_title_is_not_the_practice(self):
        """"Referral Form for Allied Health Services under Medicare" matched
        on 'Health' and was returned as the referring practice."""
        no_block = self.EPC.replace("GP details", "")
        self.assertNotIn("Referral Form",
                         str(extract_fields(no_block)["gp_practice"].value))

    def test_the_patients_address_is_not_the_practice(self):
        self.assertNotEqual(extract_fields(self.EPC)["gp_practice"].value,
                            "30 Fairlane Street")

    def test_a_real_letterhead_is_still_preferred_over_nothing(self):
        fields = extract_fields("Old Northern Road Medical Centre\n"
                                "Dr Kym R. Horsnell\n")
        self.assertEqual(fields["gp_practice"].value,
                         "Old Northern Road Medical Centre")


class ScannedBundleTests(unittest.TestCase):
    """An eight-page scanned care-plan bundle with the referral form as the
    last two pages. Both fields the reviewer needed were past the head of
    the document and had been through OCR."""

    def test_provider_number_split_by_a_space(self):
        """OCR reads the boxed number as "5839741 H"."""
        field = find_provider("GP details\nProvider No.\n5839741 H\n")
        self.assertEqual(field.value, "5839741H")
        self.assertEqual(field.confidence, OK)

    def test_provider_number_without_a_space_is_unchanged(self):
        self.assertEqual(find_provider("Provider number:040501AW").value,
                         "040501AW")

    def test_date_beyond_the_head_of_a_long_bundle(self):
        """The signature date sat on page 7 of 8, far past the 2500-char
        window that the first pass reads."""
        text = "Care plan.\n" + ("filler line\n" * 400) + "Date signed\n04/02/2025\n"
        self.assertGreater(len(text), 2500)
        self.assertEqual(find_referral_date(text, None).value, "2025-02-04")

    def test_ocr_separators_read_as_letters(self):
        text = "x" * 3000 + "\nDate signed\n04t02t2025\n"
        field = find_referral_date(text, None)
        self.assertEqual(field.value, "2025-02-04")
        self.assertEqual(field.confidence, CHECK)
        self.assertIn("CONFIRM", field.note)

    def test_ocr_zero_read_as_the_letter_o(self):
        text = "x" * 3000 + "\nDate signed\n04to2t2025\n"
        self.assertEqual(find_referral_date(text, None).value, "2025-02-04")

    def test_a_phone_number_is_not_read_as_a_date(self):
        """Ten digits, but position 2 is a real digit, so it cannot be one."""
        text = "x" * 3000 + "\nPhone\n0466590548\n"
        self.assertIsNone(find_referral_date(text, None).value)

    def test_a_medicare_number_is_not_read_as_a_date(self):
        text = "x" * 3000 + "\nMedicare No.\n4207509012\n"
        self.assertIsNone(find_referral_date(text, None).value)

    def test_the_head_still_wins_when_it_has_a_date(self):
        """The widened pass must not change a document the first pass reads:
        the footnote date further down was the bug it was written for."""
        text = ("RE: Mr Aiden Ward\n24 September 2025\n"
                + "body line\n" * 400 + "downloaded 1 July 2025\n")
        self.assertEqual(find_referral_date(text, None).value, "2025-09-24")


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
