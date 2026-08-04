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
import re
from dataclasses import dataclass, field as _field
from typing import Any, Optional

from nookal_client import medicare_number_valid, provider_number_valid

# Text below this many characters per page means there is no usable text
# layer and the page needs OCR.
MIN_CHARS_PER_PAGE = 40

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
    if pages and len(text) / pages >= MIN_CHARS_PER_PAGE:
        return text, "digital", pages
    try:
        return ocr_pdf(path), "ocr", pages
    except OcrUnavailable:
        return "", "ocr_unavailable", pages


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
RE_DOB = re.compile(r"\bDOB:?\s*\(?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{4})")
RE_DATE_NUMERIC = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
RE_DATE_LONG = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4})\b")

# 6-digit stem + practice-location char + check char. Validated afterwards,
# so a loose pattern is fine.
RE_PROVIDER_CANDIDATE = re.compile(r"\b(\d{5,6}[0-9A-Z][A-Z])\b")
RE_TEN_DIGITS = re.compile(r"\b(\d{10})\b")
RE_MEDICARE_LABELLED = re.compile(
    r"Medicare\s*(?:No|Number|#)?\.?:?\s*([\d\s]{10,14})", re.I)

RE_GP = re.compile(
    r"\bDr\.?[ \t]+([A-Z][A-Za-z'\-\.]+(?:[ \t]+[A-Z][A-Za-z'\-\.]+)?"
    r"(?:[ \t]+[A-Z][A-Za-z'\-\.]+)?)")

# Words that mean the capture has run past the name into letterhead or
# clinic detail — "Dr Jamie Sutherland KEPERRA Keperra QLD".
NAME_STOPWORDS = re.compile(
    r"\b(QLD|NSW|VIC|SA|WA|TAS|NT|ACT|Medical|Clinic|Centre|Practice|"
    r"Shop|Suite|Unit|Street|Road|Health|Phone|Fax|Visit|DOB)\b", re.I)

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


def find_provider(text: str) -> Field:
    valid = [n for n in RE_PROVIDER_CANDIDATE.findall(text)
             if provider_number_valid(n)]
    unique = list(dict.fromkeys(valid))
    if len(unique) == 1:
        return Field(unique[0], OK, "check character valid")
    if len(unique) > 1:
        return Field(unique[0], CHECK,
                     f"several found: {', '.join(unique)}")
    return Field(None, MISSING, "no valid provider number found")


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
    match = RE_PATIENT.search(text)
    if not match:
        return Field(None, MISSING, "no 'RE:' line found")
    name = trim_name(" ".join(match.group(1).split()))
    if not name or " " not in name:
        return Field(name or None, CHECK, "could not read a full name "
                     "from the 'RE:' line")
    return Field(name, CHECK, "from the 'RE:' line")


def find_dob(text: str) -> Field:
    match = RE_DOB.search(text)
    if not match:
        return Field(None, MISSING, "no DOB label found")
    iso = to_iso(match.group(1))
    if iso is None:
        return Field(match.group(1), CHECK, "could not parse as a date")
    return Field(iso, CHECK, f"read as {match.group(1)} (day/month order)")


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


def find_referral_date(text: str, dob: Optional[str]) -> Field:
    """The letter date: the earliest date in the document that isn't the
    patient's DOB. Ordered by position, not by format — one referral's
    boilerplate footnote mentions "1 July 2025", and preferring long-form
    dates picked that over the real letter date of 15/09/2025."""
    head = text[:2500]
    found = [(m.start(), m.group(1))
             for pattern in (RE_DATE_LONG, RE_DATE_NUMERIC)
             for m in pattern.finditer(head)]
    today = _dt.date.today()
    rejected: list[str] = []
    for _, raw in sorted(found):
        iso = to_iso(raw)
        if not iso or iso == dob:
            continue
        age = (today - _dt.date.fromisoformat(iso)).days
        if age < 0 or age > MAX_REFERRAL_AGE_DAYS:
            rejected.append(raw)
            continue
        return Field(iso, CHECK, f"read as {raw}")
    if rejected:
        return Field(None, MISSING, "only implausible dates found "
                                    f"({', '.join(rejected[:3])}) — likely "
                                    "mis-read; enter it by hand")
    return Field(None, MISSING, "no letter date found")


def find_gp(text: str, provider: Optional[str]) -> Field:
    """Prefer the doctor named nearest the provider number — referrals often
    name several people, but only one signs."""
    matches = list(RE_GP.finditer(text))
    if not matches:
        return Field(None, MISSING, "no 'Dr ...' found")
    if provider:
        anchor = text.find(provider)
        if anchor != -1:
            nearest = min(matches, key=lambda m: abs(m.start() - anchor))
            return Field(f"Dr {trim_name(nearest.group(1))}", CHECK,
                         "named nearest the provider number")
    return Field(f"Dr {trim_name(matches[0].group(1))}", CHECK,
                 "first doctor named")


def find_practice(text: str) -> Field:
    """Letterheads sit in the first few lines. Searching the whole document
    picked up prose like "All specialists and allied health professionals
    have been chosen..." from deep in a care plan."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:18]
    for line in lines:
        if len(line) < 6 or len(line) > 70 or line.lower().startswith("re:"):
            continue
        if line.endswith((".", ":", ",")) or line.count(" ") > 7:
            continue          # prose, not a letterhead
        low = line.lower()
        if "@" in line or low.startswith(("www.", "http", "email", "phone",
                                          "fax", "tel", "abn", "e:", "t:",
                                          "f:")):
            continue          # contact details, not a practice name
        # Compare with separators stripped: the clinic's own name appears as
        # "Embrace Movement Clinic" and as "embracemovementclinic.com.au".
        squashed = re.sub(r"[^a-z]", "", low)
        if any(re.sub(r"[^a-z]", "", m) in squashed
               for m in OWN_CLINIC_MARKERS):
            continue          # that is us, not the referrer
        if re.search(r"(medical|clinic|practice|surgery|centre|center|"
                     r"doctors|health)", line, re.I):
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
    provider = find_provider(text)
    dob = find_dob(text)
    return {
        "patient_name": find_patient(text),
        "dob": dob,
        "medicare_no": find_medicare(text),
        "gp_name": find_gp(text, provider.value),
        "gp_provider_number": provider,
        "gp_practice": find_practice(text),
        "referral_date": find_referral_date(text, dob.value),
        "services_count": find_sessions(text),
        "conditions": find_conditions(text),
    }


def extract(path: str) -> Referral:
    text, source, pages = read_text(path)
    return Referral(source=source, pages=pages, fields=extract_fields(text))
