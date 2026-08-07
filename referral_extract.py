"""
referral_extract.py — pull the CCMP fields out of a GP referral PDF.

Two input paths, chosen per file:

* **digital** — the PDF carries a text layer (Best Practice exports and
  similar). Text is read directly, so extraction is near-exact.
* **scanned** — the PDF is page images. Each page is rasterised and passed
  through Tesseract.

Of the ten sample referrals from Embrace Movement Clinic, four were digital
and six were scans; the scans were clean 300 DPI printed text, not faxes,
and OCR read the provider numbers, Medicare numbers and session counts
correctly.

Nothing here writes to Nookal. Output is a dict per referral, with a
confidence flag on every field, meant to be reviewed by a human before
anything is created.

Field confidence
----------------
    ok       validated (check digit passed) or matched an unambiguous label
    check    found, but a human should confirm it
    missing  not found — the reviewer supplies it

Medicare and provider numbers are check-digit validated, so a single
mis-read character fails validation rather than being written silently.
That is what makes OCR safe on the two highest-risk fields.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import re
from dataclasses import dataclass, field as _field
from typing import Any, Optional

from nookal_client import medicare_number_valid, provider_number_valid

# Text below this many characters per page means there is effectively no
# text layer at all.
MIN_CHARS_PER_PAGE = 40

# ...but "has some text" is not the same as "has usable text". Genuinely
# digital referrals carry 900-1500 characters a page. One scanned care plan
# carried 143 — a mangled OCR of the letterhead alone ("Health hsurarrce
# Commission"), with every real field still locked in the image. That was
# enough to be treated as digital, so OCR never ran and nothing was read.
#
# Below this, OCR runs as well and wins if it recovers more text. Comparing
# the two is what makes it safe: a real digital referral that happens to be
# sparse keeps its own text.
THIN_TEXT_PER_PAGE = 350

OK, CHECK, MISSING = "ok", "check", "missing"


@dataclass
class Field:
    value: Any = None
    confidence: str = MISSING
    note: str = ""

    def __bool__(self) -> bool:
        return self.value is not None


@dataclass
class Referral:
    source: str = ""                     # 'digital' or 'ocr'
    pages: int = 0
    fields: dict[str, Field] = _field(default_factory=dict)

    def get(self, name: str) -> Any:
        f = self.fields.get(name)
        return f.value if f else None

    def needs_review(self) -> list[str]:
        return [name for name, f in self.fields.items()
                if f.confidence != OK]

    def as_dict(self) -> dict:
        """Shape expected by nookal_client.format_case_notes."""
        return {name: f.value for name, f in self.fields.items()
                if f.value is not None}


# --------------------------------------------------------------------------
# Text acquisition
# --------------------------------------------------------------------------

class OcrUnavailable(RuntimeError):
    """Tesseract is not installed, so a scanned referral cannot be read."""


def text_layer_is_thin(chars: int, pages: int) -> bool:
    """Is this too little text to be a real digital referral?

    Genuinely digital referrals carry roughly 900-1500 characters a page.
    A scanned care plan carried 143 — a mangled OCR of the letterhead only,
    with every real field still in the image. Any threshold that calls that
    'digital' means OCR never runs and nothing is read.
    """
    if not pages:
        return True
    return chars / pages < THIN_TEXT_PER_PAGE


def read_text(path: str) -> tuple[str, str, int]:
    """Return (text, source, page_count).

    Uses pypdf, which has no dependencies at all. pdfplumber was used
    originally, but it pulls in pdfminer.six -> cryptography, which needs a
    Rust toolchain and fails to build on a stock Mac. Checked against the
    ten sample referrals: both libraries extracted every field identically.

    A scanned referral with no OCR installed comes back as
    'ocr_unavailable' rather than raising, so it still reaches the review
    queue for someone to type in by hand.
    """
    from pypdf import PdfReader

    reader = PdfReader(path)
    pages = len(reader.pages)
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    per_page = len(text) / pages if pages else 0

    if not text_layer_is_thin(len(text), pages):
        return text, "digital", pages

    try:
        scanned = ocr_pdf(path)
    except OcrUnavailable:
        if per_page >= MIN_CHARS_PER_PAGE:
            return text, "digital", pages     # thin, but it is all we have
        return "", "ocr_unavailable", pages

    if len(scanned) > len(text):
        return scanned, "ocr", pages
    return text, "digital", pages


def ocr_pdf(path: str) -> str:
    """OCR every page. Scanned referrals store one image per page, which is
    extracted directly rather than re-rasterising the whole page."""
    import subprocess
    import tempfile

    from pypdf import PdfReader

    out: list[str] = []
    reader = PdfReader(path)
    with tempfile.TemporaryDirectory() as tmp:
        for n, page in enumerate(reader.pages):
            try:
                images = list(page.images)
            except ImportError as exc:
                # pypdf needs Pillow to pull page images out. Without it the
                # whole inbox scan would die on the first scanned referral,
                # so treat it the same as a missing OCR binary.
                raise OcrUnavailable(
                    "Pillow is not installed, so scanned pages cannot be "
                    "read: pip install -r requirements-extract.txt"
                ) from exc
            if not images:
                continue
            src = f"{tmp}/page{n}"
            with open(src, "wb") as f:
                f.write(images[0].data)
            try:
                proc = subprocess.run(["tesseract", src, "-"],
                                      capture_output=True, text=True)
            except FileNotFoundError as exc:
                raise OcrUnavailable(
                    "Tesseract is not installed — scanned referrals cannot "
                    "be read until it is. Digital referrals are unaffected."
                ) from exc
            out.append(proc.stdout)
    return "\n".join(out)


# --------------------------------------------------------------------------
# Field patterns
# --------------------------------------------------------------------------

TITLES = r"(?:Mr|Mrs|Ms|Miss|Dr|Master)"

# [ \t] not \s: a name must not run across a line break, or the DOB and
# address lines underneath get swallowed into it.
RE_PATIENT = re.compile(
    rf"\bRE:[ \t]*(?:{TITLES}\.?[ \t]+)?"
    r"([A-Z][A-Za-z'\-]+(?:[ \t]+[A-Z][A-Za-z'\-]+)+)",
)
# "Patient's Name: Miss Holly Kell" on the CDM/TCA forms. Both apostrophes,
# because one real referral uses the curly one.
RE_PATIENT_LABELLED = re.compile(
    rf"Patient['’]?s?[ \t]+Name[ \t]*:?[ \t]*(?:{TITLES}\.?[ \t]+)?"
    r"([A-Z][A-Za-z'\-]+(?:[ \t]+[A-Z][A-Za-z'\-]+)+)")

# The EPC form splits the name across two labelled boxes. Where the boxes
# fall relative to the line breaks depends on how wide the typed values are,
# so all four arrangements turn up across one clinic's referrals:
#
#     First Name          First Name Jane     First Name Jane
#     Cheryl Surname Moss Surname             Surname
#                         Brooker             Brooker
#
# Each gap therefore tolerates a single newline. Only one: allowing more
# would let the capture reach past an empty First Name box into the address.
_GAP = r"[ \t]*\n?[ \t]*"
RE_PATIENT_EPC = re.compile(
    r"First[ \t]+Name[ \t]*:?" + _GAP + r"(?!Surname\b)([A-Z][A-Za-z'\-]+)"
    + _GAP + r"Surname[ \t]*:?" + _GAP + r"([A-Z][A-Za-z'\-]+)")

# Accepts "Date of Birth:" as well as "DOB:", and tolerates text between the
# label and the date — one form extracts as
# "DOB: Patient Demographics.  12/11/1960". The bare 10-digit alternative
# catches dates whose separators were mangled; see repair_date_separators.
RE_DOB = re.compile(
    r"(?:Date[ \t]+of[ \t]+Birth|\bD\.?O\.?B\.?)[ \t]*:?[^\d\n]{0,40}"
    r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{4}|\d{10})", re.I)
RE_DATE_NUMERIC = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")

# A ten-character run that might be DD/MM/YYYY with the separators mis-read.
# The scanned EPC forms render "04/02/2025" as "04t02t2025" and, on the same
# page, "04to2t2025". Matched loosely and checked for shape in
# repair_ocr_date, which is far easier to follow than one regex doing both.
DATE_CHARS = r"[\dOoDQlIi|SBZG/\-.tT]"
RE_DATE_OCR = re.compile(rf"(?<![\w/\-])({DATE_CHARS}{{10}})(?![\w/\-])")

# Only the unambiguous ones. 'a' also turns up for '0' on these scans
# ("a410212025"), but that is a guess too far — and it does not need to be
# made, because the same date appears elsewhere in the document in a form
# that reads cleanly.
OCR_DIGIT_LOOKALIKES = str.maketrans({
    "O": "0", "o": "0", "D": "0", "Q": "0",
    "l": "1", "I": "1", "i": "1", "|": "1",
    "S": "5", "B": "8", "Z": "2", "G": "6",
})
# What a mis-read '/' can come out as. '1' is included because one practice's
# PDFs render every separator as one; see repair_date_separators.
DATE_SEPARATORS = set("/-.tT1lI|iu")
RE_DATE_LONG = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4})\b")

# 6-digit stem + practice-location char + check char. Validated afterwards,
# so a loose pattern is fine.
#
# The trailing characters are allowed to be separated by spaces: OCR reads
# the boxed provider number on the scanned EPC forms as "5839741 H", and
# without this the number is simply not found. A stray candidate picked up
# by the looser pattern still has to pass the check character, which is a
# 1-in-11 accident at worst.
RE_PROVIDER_CANDIDATE = re.compile(r"\b(\d{5,6}[ \t]?[0-9A-Z][ \t]?[A-Z])\b")
RE_TEN_DIGITS = re.compile(r"\b(\d{10})\b")
RE_MEDICARE_LABELLED = re.compile(
    r"Medicare\s*(?:No|Number|#)?\.?:?\s*([\d\s]{10,14})", re.I)

RE_GP = re.compile(
    r"\bDr\.?[ \t]+([A-Z][A-Za-z'\-\.]+(?:[ \t]+[A-Z][A-Za-z'\-\.]+)?"
    r"(?:[ \t]+[A-Z][A-Za-z'\-\.]+)?)")

# Words that mean the capture has run past the name into letterhead or
# clinic detail — "Dr Jamie Sutherland KEPERRA Keperra QLD".
# On the CDM forms the next field's label sits on the same line as the
# name — "Patient's Name: Ms Cheryl Moss Date of Birth: 12/11/1960" — so
# label words have to end the capture as well as address words do.
NAME_STOPWORDS = re.compile(
    r"\b(QLD|NSW|VIC|SA|WA|TAS|NT|ACT|Medical|Clinic|Centre|Practice|"
    r"Shop|Suite|Unit|Street|Road|Health|Phone|Fax|Visit|DOB|"
    r"Date|Birth|Medicare|Contact|Surname|Provider|Number|Patient|"
    r"Details|Address|Home|Work|Mobile)\b", re.I)

WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

# Ordered most-specific first; the first match wins.
SESSION_PATTERNS = [
    re.compile(r"\((\d+)\)\s*(?:sessions?|visits?)", re.I),
    re.compile(r"\b(\d+)\s*(?:sessions?|visits?)\b", re.I),
    re.compile(rf"\b({'|'.join(WORD_NUMBERS)})\s*(?:sessions?|visits?)\b",
               re.I),
]

# The destination clinic appears in every referral's address block. Without
# excluding it, it gets picked up as the *referring* practice.
OWN_CLINIC_MARKERS = ("embrace movement",)

# The EPC form has no letterhead at all — it is a Department of Health form,
# so the referring practice is only ever in the 'GP details' block, as the
# first line of that block's Address. Anchoring on the block is what tells
# the referrer's address apart from the patient's and from ours, all three
# of which are labelled 'Address' on the same page.
# [\s\S] to cross line breaks in the run-up rather than re.S, which would
# also let the captured line run to the end of the document.
RE_GP_ADDRESS_BLOCK = re.compile(
    r"GP[ \t]+details\b[\s\S]{0,400}?"
    r"\bAddress[ \t]*:?[ \t]*\n[ \t]*(\S[^\n]*)", re.I)

RE_GP_DETAILS = re.compile(r"\bGP[ \t]+details\b", re.I)
GP_DETAILS_WINDOW = 400

# A practice web or email address. Requires a 7-character stem so that
# health.gov.au, which appears in the EPC form's own footer, cannot match.
RE_DOMAIN = re.compile(
    r"\b([A-Za-z][A-Za-z0-9]{6,60})\.(?:com|net|org)(?:\.au)?\b", re.I)

# How much of the domain the assembled name has to account for, and how
# close a single word has to be to its slice of it. Both are deliberately
# tight: the point of this route is that the domain *confirms* the name, so
# a partial match is worth nothing.
DOMAIN_COVERAGE = 0.85
DOMAIN_WORD_SIMILARITY = 0.75

# Wording that reads like a letterhead but belongs to the printed form
# rather than to any practice. The EPC form's second line — "Referral Form
# for Allied Health Services under Medicare" — matched on 'Health' and was
# returned as the referring practice on every referral of that type.
FORM_TITLE_MARKERS = re.compile(
    r"\b(referral form|enhanced primary care|allied health services?|"
    r"team care arrangements?|gp management plan|multidisciplinary care|"
    r"department of health|to be completed by|medicare claims?|"
    r"private health insurance)\b", re.I)

# A referral older than this, or dated in the future, is a mis-read rather
# than a real date — one OCR run produced 1951 for a 2025 letter.
MAX_REFERRAL_AGE_DAYS = 365 * 3

CONDITION_HEADINGS = (
    "medical/surgical history", "past medical history", "medical history",
    "current conditions", "conditions",
)
CONDITION_STOP = (
    "medications", "allergies", "social history", "family history",
    "surgical history", "cultural status", "immunisations", "management",
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def to_iso(value: str) -> Optional[str]:
    """DD/MM/YYYY (Australian order) or '24 September 2025' -> YYYY-MM-DD."""
    value = value.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return _dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def find_medicare(text: str) -> Field:
    """Labelled number first; otherwise any 10-digit run that passes the
    check digit. Australian phone numbers all start with 0, which the
    leading-digit rule (2-6) already excludes, so phones cannot collide."""
    labelled = RE_MEDICARE_LABELLED.search(text)
    if labelled:
        digits = re.sub(r"\D", "", labelled.group(1))[:10]
        if medicare_number_valid(digits):
            return Field(digits, OK, "labelled and check digit valid")
        if digits:
            return Field(digits, CHECK, "labelled but FAILS check digit — "
                                        "re-read it from the referral")
    valid = [n for n in RE_TEN_DIGITS.findall(text) if medicare_number_valid(n)]
    unique = list(dict.fromkeys(valid))
    if len(unique) == 1:
        return Field(unique[0], OK, "unlabelled, identified by check digit")
    if len(unique) > 1:
        return Field(unique[0], CHECK,
                     f"{len(unique)} valid-looking numbers found: "
                     f"{', '.join(unique)}")
    return Field(None, MISSING, "no Medicare number on the referral — take "
                                "it from the card")


def find_provider(text: str, near: Optional[str] = None) -> Field:
    """`near` is the referring GP's name, when it is already known.

    Group practices print every doctor's provider number in the letterhead
    — one referral listed eleven — so "the only valid number in the
    document" stops being a safe rule. Taking the first meant billing
    against whichever partner happened to be top-left of the page. The one
    printed beside the doctor who signed is the referral's.
    """
    # Spaces are stripped after validating: the validator ignores them, but
    # the value goes to Nookal and belongs there without them.
    found = [(m.start(), re.sub(r"\s+", "", m.group(1)))
             for m in RE_PROVIDER_CANDIDATE.finditer(text)
             if provider_number_valid(m.group(1))]
    unique = list(dict.fromkeys(n for _, n in found))
    if not unique:
        return Field(None, MISSING, "no valid provider number found")
    if len(unique) == 1:
        return Field(unique[0], OK, "check character valid")

    if near:
        anchors = [m.start() for m in re.finditer(re.escape(near), text)]
        if anchors:
            _, number = min(found, key=lambda f: min(abs(f[0] - a)
                                                     for a in anchors))
            return Field(number, CHECK,
                         f"{len(unique)} provider numbers on the referral; "
                         f"this is the one printed nearest {near}")
    return Field(unique[0], CHECK, f"several found: {', '.join(unique)}")


def find_sessions(text: str) -> Field:
    for pattern in SESSION_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1)
        count = WORD_NUMBERS.get(raw.lower(), None)
        if count is None:
            try:
                count = int(raw)
            except ValueError:
                continue
        if not 1 <= count <= 10:
            continue
        context = text[max(0, match.start() - 60):match.end() + 20]
        context = " ".join(context.split())
        # Never trusted outright: this number sets the session cap, and
        # Sessions=0 in Nookal means Unlimited.
        return Field(count, CHECK, f"from: “…{context}…”")
    return Field(None, MISSING, "no session count stated")


def find_patient(text: str) -> Field:
    """Three layouts, in order of how explicit they are.

    A GP letter says "RE: Mr Aiden Ward". The CDM/TCA forms say "Patient's
    Name: Miss Holly Kell". The EPC form splits it into First Name and
    Surname boxes. All three turn up in one clinic's referrals.
    """
    labelled = RE_PATIENT_LABELLED.search(text)
    if labelled:
        name = trim_name(" ".join(labelled.group(1).split()))
        if name and " " in name:
            return Field(name, CHECK, "from the 'Patient's Name' field")

    epc = RE_PATIENT_EPC.search(text)
    if epc:
        return Field(f"{epc.group(1)} {epc.group(2)}", CHECK,
                     "from the First Name / Surname boxes")

    match = RE_PATIENT.search(text)
    if not match:
        return Field(None, MISSING,
                     "no 'RE:', 'Patient's Name' or 'First Name/Surname' "
                     "found — type the name in")
    name = trim_name(" ".join(match.group(1).split()))
    if not name or " " not in name:
        return Field(name or None, CHECK, "could not read a full name "
                     "from the 'RE:' line")
    return Field(name, CHECK, "from the 'RE:' line")


def repair_date_separators(digits: str) -> Optional[str]:
    """Recover DD/MM/YYYY from a run of ten digits.

    One practice's PDFs render '/' as '1', so 15/08/2002 extracts as
    1510812002. Every date in that document was corrupted the same way
    (04/02/2025 as 0410212025), so the substitution is systematic rather
    than a one-off misread — but it is still a guess, and callers flag it
    for confirmation.
    """
    if len(digits) != 10 or digits[2] != "1" or digits[5] != "1":
        return None
    return f"{digits[:2]}/{digits[3:5]}/{digits[6:]}"


def repair_ocr_date(run: str) -> Optional[str]:
    """Recover DD/MM/YYYY from ten characters whose separators OCR mangled.

    Shape is what makes this safe, not the characters: positions 2 and 5
    must be separators and the other eight must read as digits. A phone
    number, a Medicare number and an item number all carry a real digit at
    position 2, so none of them can be mistaken for a date here.
    """
    if len(run) != 10:
        return None
    if run[2] not in DATE_SEPARATORS or run[5] not in DATE_SEPARATORS:
        return None
    digits = (run[:2] + run[3:5] + run[6:]).translate(OCR_DIGIT_LOOKALIKES)
    if not digits.isdigit():
        return None
    return f"{digits[:2]}/{digits[2:4]}/{digits[4:]}"


def find_dob(text: str) -> Field:
    match = RE_DOB.search(text)
    if not match:
        return Field(None, MISSING, "no date-of-birth label found")
    raw = match.group(1)
    iso = to_iso(raw)
    if iso is not None:
        return Field(iso, CHECK, f"read as {raw} (day/month order)")

    repaired = repair_date_separators(raw)
    iso = to_iso(repaired) if repaired else None
    if iso is not None:
        return Field(iso, CHECK,
                     f"the PDF gave '{raw}' with unreadable separators; read "
                     f"as {repaired} — CONFIRM against the referral")
    return Field(None, MISSING,
                 f"a date of birth was labelled but '{raw}' could not be "
                 "read as a date — type it in")


def trim_name(name: str) -> str:
    """Cut a captured name at the first word that clearly is not part of it."""
    words: list[str] = []
    for word in name.split():
        if NAME_STOPWORDS.fullmatch(word) or NAME_STOPWORDS.match(word):
            break
        # A SHOUTED word after a plausible name is letterhead, not part of
        # it ("Jamie Sutherland KEPERRA"). Allowed in the first two words,
        # because OCR renders some surnames that way ("Jill O'MALLEY").
        if len(words) >= 2 and len(word) > 1 and word.isupper():
            break
        words.append(word)
    return " ".join(words)


def scan_for_dates(window: str, ocr_tolerant: bool) -> list[tuple[int, str, str]]:
    """(position, date as DD/MM/YYYY, what the page actually said)."""
    found = [(m.start(), m.group(1), m.group(1))
             for pattern in (RE_DATE_LONG, RE_DATE_NUMERIC)
             for m in pattern.finditer(window)]
    if ocr_tolerant:
        found += [(m.start(), repaired, m.group(1))
                  for m in RE_DATE_OCR.finditer(window)
                  if (repaired := repair_ocr_date(m.group(1)))]
    return sorted(found)


def find_referral_date(text: str, dob: Optional[str]) -> Field:
    """The letter date: the earliest date that isn't the patient's DOB.

    Two passes. The first reads only the head of the document, because on a
    letter the date is at the top and the body is full of other dates.
    Ordered by position, not by format — one referral's boilerplate footnote
    mentions "1 July 2025", and preferring long-form dates picked that over
    the real letter date of 15/09/2025.

    The second pass runs only when the first finds nothing, and widens to
    the whole document with OCR-mangled separators allowed. Referrals often
    arrive as the last page or two of a scanned care-plan bundle: on one
    eight-page bundle the signature date was on pages 7 and 8, thousands of
    characters past the head, and read as "04t02t2025". Because this pass
    only ever runs where the answer was previously 'no letter date found',
    it cannot change what the first pass already gets right.
    """
    today = _dt.date.today()
    rejected: list[str] = []

    for window, ocr_tolerant in ((text[:2500], False), (text, True)):
        for _, raw, as_printed in scan_for_dates(window, ocr_tolerant):
            iso = to_iso(raw)
            if not iso or iso == dob:
                continue
            age = (today - _dt.date.fromisoformat(iso)).days
            if age < 0 or age > MAX_REFERRAL_AGE_DAYS:
                rejected.append(as_printed)
                continue
            if raw == as_printed:
                return Field(iso, CHECK, f"read as {raw}")
            return Field(iso, CHECK,
                         f"the PDF gave '{as_printed}' with unreadable "
                         f"separators; read as {raw} — CONFIRM against the "
                         "referral")

    if rejected:
        return Field(None, MISSING, "only implausible dates found "
                                    f"({', '.join(rejected[:3])}) — likely "
                                    "mis-read; enter it by hand")
    return Field(None, MISSING, "no letter date found")


def find_gp_in_block(text: str) -> Optional[Field]:
    """The doctor named in the form's own 'GP details' box.

    This is the only place on the page that is labelled as the *referring*
    GP, which is what makes it worth checking before anything else. A group
    practice's letterhead names every partner, so any rule that works
    outwards from the letterhead is picking between colleagues by position
    on the page rather than by role.
    """
    header = RE_GP_DETAILS.search(text)
    if not header:
        return None
    region = text[header.end():header.end() + GP_DETAILS_WINDOW]
    match = RE_GP.search(region)
    if not match:
        return None
    name = trim_name(match.group(1))
    if not name:
        return None
    return Field(f"Dr {name}", CHECK, "named in the form's 'GP details' box")


def find_gp(text: str, provider: Optional[str]) -> Field:
    """Prefer the doctor named nearest the provider number — referrals often
    name several people, but only one signs."""
    matches = list(RE_GP.finditer(text))
    if not matches:
        return Field(None, MISSING, "no 'Dr ...' found")
    if provider:
        # Whitespace-tolerant, because the provider number is stored with
        # the OCR's spaces stripped and so no longer matches the page text.
        found = re.search(r"[ \t]*".join(map(re.escape, provider)), text)
        anchor = found.start() if found else -1
        if anchor != -1:
            nearest = min(matches, key=lambda m: abs(m.start() - anchor))
            return Field(f"Dr {trim_name(nearest.group(1))}", CHECK,
                         "named nearest the provider number")
    return Field(f"Dr {trim_name(matches[0].group(1))}", CHECK,
                 "first doctor named")


PRACTICE_WORDS = re.compile(
    r"(medical|clinic|practice|surgery|centre|center|doctors|health)", re.I)


def is_our_own_clinic(line: str) -> bool:
    """Compared with separators stripped: the clinic's own name appears as
    "Embrace Movement Clinic" and as "embracemovementclinic.com.au"."""
    squashed = re.sub(r"[^a-z]", "", line.lower())
    return any(re.sub(r"[^a-z]", "", m) in squashed for m in OWN_CLINIC_MARKERS)


def looks_like_a_practice_name(line: str) -> bool:
    """Shared by both routes below: is this line a practice name at all?"""
    if len(line) < 6 or len(line) > 70 or line.lower().startswith("re:"):
        return False
    if line.endswith((".", ":", ",")) or line.count(" ") > 7:
        return False          # prose, not a letterhead
    low = line.lower()
    if "@" in line or low.startswith(("www.", "http", "email", "phone",
                                      "fax", "tel", "abn", "e:", "t:", "f:")):
        return False          # contact details, not a practice name
    if is_our_own_clinic(line):
        return False          # that is us, not the referrer
    if FORM_TITLE_MARKERS.search(line):
        return False          # the form's own wording, not a letterhead
    return bool(PRACTICE_WORDS.search(line))


def assemble_against(stem: str, words: list[str]) -> tuple[list[str], int]:
    """Consume `stem` left to right with `words`, in order, skipping any
    that do not fit. Returns the words used and how much was consumed."""
    picked: list[str] = []
    pos = 0
    for word in words:
        if pos >= len(stem):
            break
        slice_ = stem[pos:pos + len(word)]
        if len(slice_) < 3:
            continue
        # The word has to fit in what is left, give or take the one
        # character OCR tends to drop. Without this the domain itself —
        # which appears in the letterhead as a word — matches whatever
        # remains of the stem and scores better than the real name.
        if len(slice_) < len(word) - 1:
            continue
        if (difflib.SequenceMatcher(None, word.lower(), slice_).ratio()
                >= DOMAIN_WORD_SIMILARITY):
            picked.append(word)
            pos += len(word)
    return picked, min(pos, len(stem))


def practice_name_from_domain(lines: list[str]) -> Optional[str]:
    """Rebuild a practice name that OCR broke apart, using the practice's
    own domain to prove the result.

    Scanned letterheads put the logo and the address in two columns, and
    OCR reads across both. One clinic's name came out split down two lines
    and interleaved with its address:

        [Al CASTLE HILL MURRUMBA DOWNS @4502
        bik MEDICAL CENTRE P: 073886 5100

    No line of that is the practice name, and picking the better-looking
    one gives "MEDICAL CENTRE". But the same letterhead carries
    "castlehillmedicaicentre.com", and walking that stem through the
    letterhead's words in order — taking CASTLE, HILL, MEDICAL, CENTRE and
    skipping MURRUMBA, DOWNS and the phone number — reassembles the name
    and confirms it at the same time. Nothing is guessed: every word comes
    off the page, and the domain decides which ones and in what order.
    """
    # Screen our own domain out here rather than on the assembled name: a
    # partial assembly of "embracemovementclinic" comes out as "Movement
    # Clinic", which no longer looks like us but still is.
    stems = [m.group(1).lower() for line in lines
             for m in RE_DOMAIN.finditer(line)
             if not is_our_own_clinic(m.group(1))]
    if not stems:
        return None
    words = [w for line in lines
             for w in re.findall(r"[A-Za-z][A-Za-z'&\-]+", line)]

    best: Optional[str] = None
    # Coverage first, then word count: a domain read two ways can produce
    # "Castle Hill Medical Centre" and "Hill Medical Centre" from the same
    # letterhead, both accounting for all of their stem. The fuller name is
    # the one on the page.
    best_score = (0.0, 0)
    for stem in stems:
        # The stem may lead with an email local part — "info@" arrives as
        # "infoa" once OCR has had the '@'. Try starting a little way in.
        for start in range(min(11, len(stem))):
            target = stem[start:]
            picked, consumed = assemble_against(target, words)
            score = (consumed / len(target) if target else 0, len(picked))
            if len(picked) < 2 or score[0] < DOMAIN_COVERAGE:
                continue
            name = " ".join(picked)
            if not PRACTICE_WORDS.search(name) or score <= best_score:
                continue
            best, best_score = name, score

    if best is None:
        return None
    # Letterheads are set in capitals; the name is going into a patient
    # record, so give it back in the case a human would write it.
    return best.title() if best.isupper() else best


def find_practice(text: str) -> Field:
    """The 'GP details' address block first, then the letterhead.

    The block is the stronger signal — it is labelled as the referrer's, so
    it cannot be confused with the patient's address or ours — but only the
    EPC-style forms have one. Letters carry a letterhead instead, and that
    sits in the first few lines: searching the whole document picked up
    prose like "All specialists and allied health professionals have been
    chosen..." from deep in a care plan.
    """
    block = RE_GP_ADDRESS_BLOCK.search(text)
    if block:
        first = block.group(1).strip()
        if looks_like_a_practice_name(first):
            return Field(first, CHECK, "first line of the GP address block")

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:18]

    from_domain = practice_name_from_domain(lines)
    if from_domain and not is_our_own_clinic(from_domain):
        return Field(from_domain, CHECK,
                     "rebuilt from the letterhead and confirmed against the "
                     "practice's own web address")

    for line in lines:
        if looks_like_a_practice_name(line):
            return Field(line, CHECK, "letterhead line near the top")
    return Field(None, MISSING, "no practice name found")


def find_conditions(text: str) -> Field:
    lines = [ln.strip() for ln in text.splitlines()]
    collected: list[str] = []
    capturing = False
    for line in lines:
        low = line.lower().rstrip(":").strip()
        if any(low.startswith(h) for h in CONDITION_HEADINGS):
            capturing = True
            continue
        if capturing:
            if not line:
                continue
            if any(low.startswith(s) for s in CONDITION_STOP):
                break
            cleaned = re.sub(r"^[•\-\*•]\s*", "", line)
            cleaned = re.sub(r"^\d{1,2}/\d{1,2}/\d{4}\s+", "", cleaned)
            if cleaned and len(cleaned) > 2:
                collected.append(cleaned)
            if len(collected) >= 12:
                break
    if collected:
        return Field(collected, CHECK, f"{len(collected)} line(s) under a "
                                       "history heading")
    return Field(None, MISSING, "no history section found")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def extract_fields(text: str) -> dict[str, Field]:
    # Name first when the form labels it, so the provider number can be
    # chosen by whose it is rather than by where it sits on the page. Only
    # falls back to the old order — number first, then the doctor nearest
    # it — on referrals that have no 'GP details' box to read.
    named = find_gp_in_block(text)
    provider = find_provider(text, near=named.value if named else None)
    gp = named or find_gp(text, provider.value)
    dob = find_dob(text)
    return {
        "patient_name": find_patient(text),
        "dob": dob,
        "medicare_no": find_medicare(text),
        "gp_name": gp,
        "gp_provider_number": provider,
        "gp_practice": find_practice(text),
        "referral_date": find_referral_date(text, dob.value),
        "services_count": find_sessions(text),
        "conditions": find_conditions(text),
    }


def extract(path: str) -> Referral:
    text, source, pages = read_text(path)
    return Referral(source=source, pages=pages, fields=extract_fields(text))
