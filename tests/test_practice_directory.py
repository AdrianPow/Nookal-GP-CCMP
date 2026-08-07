"""The practice directory: keys off referrals, fuzzy lookup, learning from
confirmed reviews, and the pipeline hook that fills the practice name.

The fixture text is the real OCR of a Castle Hill Medical Centre referral —
the case that motivated all of this: no line of the letterhead held the
practice name, and the phone numbers and provider numbers were the only
things OCR read reliably.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from practice_directory import (PracticeDirectory, extract_practice_keys,
                                normalise_phone)
from referral_pipeline import ReviewItem, apply_practice_lookup

CASTLE_HILL = (
    "tome A> Shop 5/272 Dohtes Rocks Read\n"
    "[Al CASTLE HILL MURRUMBA DOWNS @4502\n"
    "bik MEDICAL CENTRE P: 073886 5100\n"
    "F: 073886 5300\n"
    "E:  Infoacastlehillmedicaicentre.com\n"
    "W. castehilimedicaicente.com\n"
    "DR DUNCAN LYALL PROVIDER NO:2979223W\n"
    "Dr BELINDA MCDONALD PROVIDER NO: 5590695B\n"
    "10/06/2025\n"
    "GP details\n"
    "Dr Emily Watters\n"
    "Provider No: 5778858B\n"
    "Patient Details\n"
    "First Name TANYA\n"
    "Ph: 0466590548\n")


class KeyExtractionTests(unittest.TestCase):
    def test_letterhead_phones_and_fax(self):
        keys = extract_practice_keys(CASTLE_HILL)
        self.assertEqual(keys["phones"], ["0738865100", "0738865300"])

    def test_a_mobile_is_never_a_practice_key(self):
        """On these forms a bare 04 number is almost always the patient's,
        and a patient's number stored as a practice key would mis-match
        their next referral if a different practice sends it."""
        self.assertIsNone(normalise_phone("0466 590 548"))
        keys = extract_practice_keys("P: 0466 590 548\nMEDICAL CENTRE\n")
        self.assertEqual(keys["phones"], [])

    def test_phones_outside_the_letterhead_are_ignored(self):
        text = ("Some Practice\n" + "line\n" * 20
                + "Ph: 07 3888 1234\n")
        self.assertEqual(extract_practice_keys(text)["phones"], [])

    def test_domains_come_through_lowercased(self):
        keys = extract_practice_keys(CASTLE_HILL)
        self.assertIn("castehilimedicaicente", keys["domains"])

    def test_our_own_domain_is_not_a_key(self):
        keys = extract_practice_keys(
            "W: embracemovementclinic.com.au\nSOME LETTERHEAD\n")
        self.assertEqual(keys["domains"], [])

    def test_our_own_mangled_email_domain_is_not_a_key(self):
        """A scanned copy of the clinic's own abbreviated email arrives as
        'embrqacemc.com.au'. Stored as a key, it would match whichever
        practice was confirmed first to every letter addressed to us."""
        keys = extract_practice_keys(
            "Email: nicole@embrqacemc.com.au\nSOME LETTERHEAD\n")
        self.assertEqual(keys["domains"], [])

    def test_only_validated_provider_numbers_are_keys(self):
        keys = extract_practice_keys(CASTLE_HILL)
        self.assertIn("5778858B", keys["provider_numbers"])
        self.assertNotIn("1234567X", keys["provider_numbers"])

    def test_the_gp_address_block_is_a_key(self):
        text = ("GP details\nName\nDr Katya Groeneveld\n"
                "Address\n2/956 Gympie Road\nABN: 41 107 480 400\n"
                "CHERMSIDE  4032\n")
        keys = extract_practice_keys(text)
        self.assertEqual(keys["addresses"], ["2956gympieroad"])

    def test_the_street_line_is_the_key_not_the_practice_name_line(self):
        """Some blocks lead with the practice name; the street line below
        it is what identifies the spot."""
        text = ("GP details\nName\nDr Maya Venkatesh\nAddress\n"
                "Brendale Medical Centre\n249b Leitchs Road\n"
                "Brendale  4500\n")
        keys = extract_practice_keys(text)
        self.assertEqual(keys["addresses"], ["249bleitchsroad"])

    def test_a_form_label_is_never_an_address_key(self):
        """On some scans OCR tears the labels away from their values, so
        the line after 'Address' is the next form label. Stored as a key,
        'Patient Details' would match every referral with the same
        degenerate layout to whichever practice was confirmed first."""
        text = ("GP details\nProvider No.\nName\nAddress\n"
                "Patient details\nMedicare No.\n")
        self.assertEqual(extract_practice_keys(text)["addresses"], [])


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "practices.json")
        self.directory = PracticeDirectory(self.path)

    def keys(self, **overrides):
        base = {"phones": [], "domains": [], "provider_numbers": [],
                "addresses": []}
        base.update(overrides)
        return base

    def test_lookup_by_exact_phone(self):
        self.directory.learn("Castle Hill Medical Centre",
                             self.keys(phones=["0738865100"]))
        match = self.directory.lookup(self.keys(phones=["0738865100"]))
        self.assertEqual(match.name, "Castle Hill Medical Centre")
        self.assertIn("phone", match.matched_on)

    def test_lookup_by_corrupted_domain(self):
        """Two scans of the same letterhead corrupt the domain differently
        — neither equals the other, but both stay close enough to match."""
        self.directory.learn("Castle Hill Medical Centre",
                             self.keys(domains=["castehilimedicaicente"]))
        match = self.directory.lookup(
            self.keys(domains=["castlehillmedicalcentre"]))
        self.assertEqual(match.name, "Castle Hill Medical Centre")

    def test_a_different_practices_domain_does_not_match(self):
        """Sharing 'medicalcentre' is not enough material."""
        self.directory.learn("Castle Hill Medical Centre",
                             self.keys(domains=["castlehillmedicalcentre"]))
        self.assertIsNone(self.directory.lookup(
            self.keys(domains=["brendalemedicalcentre"])))

    def test_lookup_by_ocr_mangled_address(self):
        self.directory.learn("Castle Hill Medical Centre",
                             self.keys(addresses=["5272dohlesrocksrd"]))
        match = self.directory.lookup(
            self.keys(addresses=["5272dohtesrocksread"]))
        self.assertEqual(match.name, "Castle Hill Medical Centre")

    def test_stronger_keys_beat_a_stale_phone(self):
        """A practice moved and its old number was reissued: the provider
        numbers on the referral out-vote the phone."""
        self.directory.learn("Old Tenant Medical",
                             self.keys(phones=["0733331111"]))
        self.directory.learn("New Practice Clinic",
                             self.keys(provider_numbers=["5778858B"]))
        match = self.directory.lookup(self.keys(
            phones=["0733331111"], provider_numbers=["5778858B"]))
        self.assertEqual(match.name, "New Practice Clinic")

    def test_confirmations_accumulate_and_keys_merge(self):
        self.directory.learn("Castle Hill Medical Centre",
                             self.keys(phones=["0738865100"]))
        self.directory.learn("castle hill medical centre",
                             self.keys(phones=["0738865300"]))
        (record,) = self.directory.records
        self.assertEqual(record.confirmed, 2)
        self.assertEqual(sorted(record.keys["phones"]),
                         ["0738865100", "0738865300"])

    def test_the_most_recent_casing_wins(self):
        self.directory.learn("CASTLE HILL MEDICAL CENTRE", self.keys(
            phones=["0738865100"]))
        self.directory.learn("Castle Hill Medical Centre", self.keys(
            phones=["0738865100"]))
        self.assertEqual(self.directory.records[0].name,
                         "Castle Hill Medical Centre")

    def test_nothing_is_learned_without_keys_or_name(self):
        self.directory.learn("", self.keys(phones=["0738865100"]))
        self.directory.learn("Some Practice", self.keys())
        self.assertEqual(self.directory.records, [])
        self.assertFalse(os.path.exists(self.path))

    def test_survives_a_restart(self):
        self.directory.learn("Castle Hill Medical Centre",
                             self.keys(phones=["0738865100"]))
        reloaded = PracticeDirectory(self.path)
        match = reloaded.lookup(self.keys(phones=["0738865100"]))
        self.assertEqual(match.name, "Castle Hill Medical Centre")

    def test_a_corrupt_file_means_an_empty_directory_not_a_crash(self):
        with open(self.path, "w") as f:
            f.write("{ not json")
        self.assertEqual(PracticeDirectory(self.path).records, [])


class PipelineHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = PracticeDirectory(
            os.path.join(self.tmp.name, "practices.json"))
        self.directory.learn("Castle Hill Medical Centre",
                             extract_practice_keys(CASTLE_HILL))

    def item(self, practice=None, keys=None) -> ReviewItem:
        fields = {"gp_practice": practice} if practice else {}
        return ReviewItem(id="x", pdf_path="x", fields=fields,
                          practice_keys=keys or extract_practice_keys(
                              CASTLE_HILL))

    def test_fills_a_missing_practice_name(self):
        item = self.item()
        apply_practice_lookup(item, self.directory)
        self.assertEqual(item.fields["gp_practice"],
                         "Castle Hill Medical Centre")
        self.assertEqual(item.confidence["gp_practice"], "check")
        self.assertIn("recognised by", item.notes["gp_practice"])

    def test_corrects_letterhead_garbage_but_keeps_it_in_the_note(self):
        item = self.item(practice="bk MEDICAL CENTRE P: 073886 5100")
        apply_practice_lookup(item, self.directory)
        self.assertEqual(item.fields["gp_practice"],
                         "Castle Hill Medical Centre")
        self.assertIn("bk MEDICAL CENTRE", item.notes["gp_practice"])
        self.assertIn("check it hasn't changed", item.notes["gp_practice"])

    def test_an_agreeing_extraction_is_noted_not_rewritten(self):
        item = self.item(practice="Castle Hill Medical Centre")
        apply_practice_lookup(item, self.directory)
        self.assertIn("matches the practice directory",
                      item.notes["gp_practice"])

    def test_an_address_fallback_is_a_fill_not_a_disagreement(self):
        """When extraction fell back to the street address and the
        directory knows who is at that address, the name simply fills in —
        the note must not accuse the referral of 'reading' differently."""
        self.directory.learn("Chermside Family Practice",
                             {"phones": [], "domains": [],
                              "provider_numbers": ["420427AA"],
                              "addresses": ["2956gympieroad"]})
        item = ReviewItem(
            id="x", pdf_path="x",
            fields={"gp_practice": "2/956 Gympie Road, CHERMSIDE 4032"},
            practice_keys={"phones": [], "domains": [],
                           "provider_numbers": ["420427AA"],
                           "addresses": ["2956gympieroad"]})
        apply_practice_lookup(item, self.directory)
        self.assertEqual(item.fields["gp_practice"],
                         "Chermside Family Practice")
        self.assertIn("not readable on this referral",
                      item.notes["gp_practice"])
        self.assertNotIn("check it hasn't changed", item.notes["gp_practice"])

    def test_an_unknown_practice_is_left_alone(self):
        item = self.item(practice="Brendale Medical Centre",
                         keys={"phones": ["0733001122"], "domains": [],
                               "provider_numbers": ["040501AW"],
                               "addresses": []})
        apply_practice_lookup(item, self.directory)
        self.assertEqual(item.fields["gp_practice"],
                         "Brendale Medical Centre")
        self.assertNotIn("gp_practice", item.notes)


if __name__ == "__main__":
    unittest.main()
