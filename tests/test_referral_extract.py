"""Field extraction, tested on text fixtures taken from the real referrals.

No PDFs and no OCR here — those need pdfplumber/pypdf/tesseract. These
cover the pattern matching and validation, which is where the mistakes that
would reach a patient record get made.
"""

from __future__ import annotations

import unittest

from referral_extract import (CHECK, MISSING, OK, extract_fields,
                              find_medicare, find_provider, find_referral_date,
                              find_sessions, practice_name_from_domain,
                              text_layer_is_thin, to_iso, trim_name)

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


class GroupPracticeTests(unittest.TestCase):
    """A scanned referral from an eleven-doctor practice. The letterhead
    names every partner with their provider number, and OCR read the logo
    and the address as one interleaved column."""

    DAY = (
        "tome A> Shop 5/272 Dohtes Rocks Read\n"
        "[Al CASTLE HILL MURRUMBA DOWNS @4502\n"
        "bik MEDICAL CENTRE P: 073886 5100\n"
        "F: 073886 5300\n"
        "E:  Infoacastlehillmedicaicentre.com\n"
        "W. castehilimedicaicente.com\n"
        "DR DUNCAN LYALL DR PETER BARTLETT DR GERALYN McCARRON\n"
        "B.Sc., B.ECON. PROVIDER NO:2979223W PROVIDER NO: 0804844X\n"
        "Dr BELINDA MCDONALD DR EMILY WATTERS DR ASHLEY ROSE\n"
        "PROVIDER NO: 5590695B PROVIDER NO: 5778858B PROVIDER NO:5617349W\n"
        "10/06/2025\n"
        "Re: Team Care Arrangements for: Ms TANYA DAY16/09/1988\n"
        "Yours sincerely\n"
        "Dr Emily Watters\n"
        "Provider No: 5778858B\n"
        "GP details\n"
        "Dr Emily Watters\n"
        "Name\n"
        "5/272 Dohles Rocks Rd\n"
        "Address MURRUMBA DOWNS QLD 4503\n"
        "Patient Details\n"
        "Medicare Number 4334581426\n")

    def test_the_signing_doctor_not_a_partner(self):
        """Two accidents compounded: the first of eleven provider numbers
        was taken, and the letterhead names are shouted, so the one partner
        OCR happened to render as "Dr BELINDA MCDONALD" was the only match
        near it."""
        self.assertEqual(extract_fields(self.DAY)["gp_name"].value,
                         "Dr Emily Watters")

    def test_the_signing_doctors_provider_number(self):
        fields = extract_fields(self.DAY)
        self.assertEqual(fields["gp_provider_number"].value, "5778858B")
        self.assertIn("nearest Dr Emily Watters",
                      fields["gp_provider_number"].note)

    def test_several_numbers_are_never_reported_as_certain(self):
        self.assertEqual(
            extract_fields(self.DAY)["gp_provider_number"].confidence, CHECK)

    def test_practice_name_rebuilt_across_the_split_letterhead(self):
        """No single line holds the name: "CASTLE HILL" and "MEDICAL
        CENTRE" are on different lines, each interleaved with the address."""
        self.assertEqual(extract_fields(self.DAY)["gp_practice"].value,
                         "Castle Hill Medical Centre")

    def test_the_domain_has_to_account_for_the_whole_name(self):
        """Without a domain to confirm it, nothing is assembled — the
        letterhead scan handles it instead, and gets what it gets."""
        no_domain = "\n".join(ln for ln in self.DAY.splitlines()
                              if "castlehill" not in ln.lower()
                              and "castehili" not in ln.lower())
        self.assertNotEqual(extract_fields(no_domain)["gp_practice"].value,
                            "Castle Hill Medical Centre")

    def test_our_own_domain_is_never_assembled(self):
        """A partial assembly of "embracemovementclinic" reads as "Movement
        Clinic", which no longer looks like us — so the domain is screened
        out before anything is built from it."""
        lines = ["Embrace Movement Clinic", "W: embracemovementclinic.com.au"]
        self.assertIsNone(practice_name_from_domain(lines))

    def test_a_single_provider_number_is_still_certain(self):
        self.assertEqual(find_provider("Provider number:040501AW").confidence,
                         OK)


class NineReferralSweepTests(unittest.TestCase):
    """Fixtures taken from the full set of nine test referrals — each one
    is the OCR text that made a field come out wrong."""

    # Document (7): a four-doctor letterhead, each partner beside their own
    # provider number, and the actual referrer only named at the sign-off.
    MEDICROSS = (
        "medicross\n"
        "MEDICAL\n"
        "Strathpine Doctors T/A Medicross Strathpine\n"
        "Strathpine Centre 65, 295 Gympie Road Strathpine QLD 4500\n"
        "Phone: 07 3881 3828 Fax: 07 3881 2134\n"
        "Dr Kambiz Dara (MD,FRACGP) - Provider No. 4068296B\n"
        "Dr Brant Bosch (MBChB, FRACGP) -Provider No. 2676205H\n"
        "Dr Aykari Lynn, (MBBS, AMC CERT, FRACGP) - Provider No. 4638503K\n"
        "Dr Cho Cho Mar (MBBS, FRACGP) - Provider No. 5157745L\n"
        "06/03/2026\n"
        "RE: Mr Murray JONES\n"
        "DOB: 19/07/1957\n"
        "Dear Physiotherapist,\n"
        "Thank you for accepting the referral of Murray JONES.\n"
        "Yours faithfully,\n"
        ";\n"
        "Dr Cho Cho Mar A\n"
        "MBBS, FRACGP J -\n"
        "5157745L\n")

    def test_the_signing_doctor_beats_the_letterhead_list(self):
        self.assertEqual(extract_fields(self.MEDICROSS)["gp_name"].value,
                         "Dr Cho Cho Mar")

    def test_the_signers_own_provider_number_is_chosen(self):
        """The number for a doctor is printed AFTER their name, so plain
        nearest-by-distance picked the previous doctor's number, which
        ends just before the anchor."""
        field = extract_fields(self.MEDICROSS)["gp_provider_number"]
        self.assertEqual(field.value, "5157745L")

    def test_a_logo_fragment_is_not_the_practice(self):
        """OCR reads the graphic as its own line: a bare 'MEDICAL' matched
        first and hid the real name two lines below."""
        self.assertEqual(extract_fields(self.MEDICROSS)["gp_practice"].value,
                         "Strathpine Doctors T/A Medicross Strathpine")

    # Maxwell Briggs: the letterhead is a shredded graphic, but the letter
    # closes with a full signature block.
    def test_practice_named_under_the_signature(self):
        text = ("NUND AH VILL AGE 4270 Sandgate Road, Nundah 4012\n"
                "FAMILY PRACTICE E: reception@nvfp.com.au\n"
                "Yours sincerely,\n"
                "Dr Murtaza Dungerwalla 6076049W\n"
                "Nundah Village Family Practice\n"
                "ABN 35628 990 956\n")
        fields = extract_fields(text)
        self.assertEqual(fields["gp_practice"].value,
                         "Nundah Village Family Practice")

    # Karen Buckle: the practice is never named — the signature block goes
    # doctor, then shopping-centre address.
    def test_an_address_under_the_signature_is_not_a_name(self):
        text = ("Yours faithfully,\n"
                "Dr Suzanne Thomson,\n\n"
                "Shop 87 Brookside Shopping Centre\n\n"
                "159 Osborne Raad\n")
        field = extract_fields(text)["gp_practice"]
        self.assertEqual(field.value, "Shop 87 Brookside Shopping Centre")
        self.assertIn("street address", field.note)

    # Paige Barker: boxes in Surname / First Name order, and the
    # government letterhead above them.
    BARKER = ("{Australian Government\n"
              "Health Insurance Commission\n"
              "Enhanced Primary Care (EPC) Program\n"
              "GP details\n"
              "Provider Number 5778858B |\n"
              "Dr Emily Watters\n"
              "Name\n"
              "5/272 Dohles Rocks Rd\n"
              "Address MURRUMBA DOWNS QLD 4503\n"
              "Patient Details\n"
              "Medicare Number 4242940619\n"
              "[~ Surname BARKER\n"
              "First Name PAIGE\n")

    def test_surname_first_name_boxes_in_reverse_order(self):
        self.assertEqual(extract_fields(self.BARKER)["patient_name"].value,
                         "PAIGE BARKER")

    def test_the_government_letterhead_is_never_the_practice(self):
        self.assertNotEqual(extract_fields(self.BARKER)["gp_practice"].value,
                            "Health Insurance Commission")

    def test_address_collected_across_torn_labels(self):
        """The scanned block interleaves bare labels with the values, and
        the suburb line arrives welded to its label."""
        self.assertEqual(extract_fields(self.BARKER)["gp_practice"].value,
                         "5/272 Dohles Rocks Rd, MURRUMBA DOWNS QLD 4503")

    # Care Plan page 1: column-wise OCR welds the form's NOTE column onto
    # the address lines.
    def test_form_boilerplate_is_cut_off_the_address(self):
        text = ("Enhanced Primary Care (EPC) Program\n"
                "Dr Michael Bailey NOTE: Relevant MBS item(s) above must be\n"
                "17 Sparkes Road BILLED by GP prior to patient receiving their\n"
                "\n"
                "BRAY PARK 4500 first referred allied health service\n"
                "4082816K\n")
        field = extract_fields(text)["gp_practice"]
        self.assertEqual(field.value, "17 Sparkes Road, BRAY PARK 4500")


class AddressFallbackTests(unittest.TestCase):
    """Some forms never name the referring practice at all. The street
    address is still worth more than an empty field, and the practice
    directory can replace it with the real name later."""

    MOSS = ("Enhanced Primary Care (EPC) Program \n"
            "Referral Form for Allied Health Services under Medicare \n"
            "GP details : \n"
            "Provider Number 420427AA NOTE: Relevant MBS item(s)\n"
            "Name\nDr Katya Groeneveld\n"
            "Address\n2/956 Gympie Road\nABN: 41 107 480 400\n"
            "CHERMSIDE  4032\n"
            "Patient Details\nMedicare Number\n3349170497\n")

    def test_address_used_when_no_name_exists(self):
        field = extract_fields(self.MOSS)["gp_practice"]
        self.assertEqual(field.value, "2/956 Gympie Road, CHERMSIDE 4032")
        self.assertIn("street address", field.note)

    def test_the_abn_line_is_not_part_of_the_address(self):
        self.assertNotIn("ABN",
                         extract_fields(self.MOSS)["gp_practice"].value)

    def test_a_named_practice_is_never_displaced_by_its_address(self):
        text = self.MOSS.replace("2/956 Gympie Road",
                                 "Chermside Medical Centre\n"
                                 "2/956 Gympie Road")
        self.assertEqual(extract_fields(text)["gp_practice"].value,
                         "Chermside Medical Centre")

    def test_address_under_the_gp_name_when_labels_are_torn_away(self):
        """OCR of the scanned EPC bundle reads all the labels in one column
        and all the values in another, so the labelled block is unreadable
        — but the address still sits directly under the doctor's name."""
        text = ("GP details\nProvider No.\nName\nAddress\n"
                "Patient details\nMedicare No.\n"
                "5839741 H\nDr Gabriela Popa\n"
                "Shop 2, 1 Queen Elizabeth Drv Eatons Hill QLD 4037\n")
        field = extract_fields(text)["gp_practice"]
        self.assertEqual(field.value,
                         "Shop 2, 1 Queen Elizabeth Drv Eatons Hill QLD 4037")

    def test_the_patients_address_is_out_of_reach(self):
        """The patient's address looks identical — anchoring on the
        doctor's name is what keeps it out."""
        text = ("GP details\nName\nDr Gabriela Popa\nreferral text\n"
                "Patient Details\nFirst Name Holly\n"
                "9 Windrush Close Eatons Hill QLD 4037\n")
        self.assertIsNone(extract_fields(text)["gp_practice"].value)


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
