"""
referral_pipeline.py — referral queue, and the write sequence into Nookal.

A referral moves through these states:

    NEEDS_REVIEW   extracted; a human must confirm the fields
    NEEDS_PAYER    created in Nookal; the payer still has to be added by
                   hand in the UI, because that is where the session cap
                   lives and Sessions=0 silently means Unlimited
    DONE           payer added, ticked off by the operator
    BLOCKED        cannot proceed without a human decision (an ambiguous
                   patient match, a missing mandatory field)

Nothing moves out of NEEDS_REVIEW on its own. Approval is always a person
clicking, never a confidence score clearing a threshold.

The queue is a directory of JSON files, one per referral, with the source
PDF kept alongside. No database, nothing to administer, and the state
survives a restart of the clinic PC.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field as _field
from typing import Any, Optional

from nookal_client import (NookalClient, NookalError, format_case_notes,
                           medicare_number_valid)

NEEDS_REVIEW = "needs_review"
NEEDS_PAYER = "needs_payer"
DONE = "done"
BLOCKED = "blocked"

# MBS default under the CCM arrangements when the referral states no
# session count: 5 per calendar year (confirmed by the clinic 2026-08-03).
#
# Clinic policy, same date: tracking the remaining entitlement is the
# patient's responsibility, not the clinic's. So where a referral is silent,
# 5 is simply used as the cap — if the patient's real entitlement is lower,
# that is theirs to manage. This is a standing default, not a guess needing
# careful confirmation.
#
# Zero is a different matter and is never acceptable: in Nookal 0 does not
# mean "no sessions", it means UNLIMITED, which removes the cap entirely
# rather than setting it to 5.
DEFAULT_SESSIONS = 5

# Placeholder id used under dry_run, so a rehearsal can walk the whole flow
# — including the payer hand-off screen — without writing anything. Real
# runs never see it.
DRY_RUN_ID = 0

# Fields a human must have supplied before anything is written to Nookal.
# Everything else is optional: a referral with no Medicare number is normal
# (it comes off the card at reception), and roughly half state no session
# count at all.
REQUIRED = ("patient_name", "dob")


@dataclass
class ReviewItem:
    id: str
    pdf_path: str
    state: str = NEEDS_REVIEW
    source: str = ""                    # 'digital' or 'ocr'
    sha256: str = ""                    # content hash, for duplicate PDFs
    pages: int = 0
    received: str = ""
    fields: dict[str, Any] = _field(default_factory=dict)
    confidence: dict[str, str] = _field(default_factory=dict)
    notes: dict[str, str] = _field(default_factory=dict)
    nookal: dict[str, Any] = _field(default_factory=dict)
    message: str = ""
    # Set by the operator choosing "Create anyway" when the patient already
    # has a GP CCMP case.
    allow_duplicate_case: bool = False
    # Phone numbers, web domains, provider numbers and the GP address found
    # on the referral — kept so the practice directory can learn from this
    # referral once a reviewer confirms it. See practice_directory.py.
    practice_keys: dict[str, Any] = _field(default_factory=dict)

    # ------------------------------------------------------------ helpers

    @property
    def display_name(self) -> str:
        return self.fields.get("patient_name") or os.path.basename(
            self.pdf_path)

    def missing_required(self) -> list[str]:
        return [name for name in REQUIRED if not self.fields.get(name)]

    def flagged(self) -> list[str]:
        """Fields a reviewer should look at: anything not check-digit
        validated, plus anything absent."""
        return [name for name, level in self.confidence.items()
                if level != "ok"]


def file_sha256(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Queue storage
# --------------------------------------------------------------------------

class Queue:
    def __init__(self, root: str = "queue", directory=None):
        self.root = root
        # A practice_directory.PracticeDirectory, when the server provides
        # one: referrals get their practice name filled from it as they
        # arrive. Optional so nothing else that builds a Queue changes.
        self.directory = directory
        os.makedirs(self.root, exist_ok=True)

    def _path(self, item_id: str) -> str:
        return os.path.join(self.root, f"{item_id}.json")

    def add_pdf(self, pdf_path: str) -> ReviewItem:
        """Extract a referral and put it in the queue for review."""
        import referral_extract

        referral = referral_extract.extract(pdf_path)
        item = ReviewItem(
            id=uuid.uuid4().hex[:12],
            pdf_path=os.path.abspath(pdf_path),
            sha256=file_sha256(pdf_path),
            source=referral.source,
            pages=referral.pages,
            received=_dt.datetime.now().isoformat(timespec="seconds"),
            fields={k: f.value for k, f in referral.fields.items()
                    if f.value is not None},
            confidence={k: f.confidence for k, f in referral.fields.items()},
            notes={k: f.note for k, f in referral.fields.items() if f.note},
        )
        from practice_directory import extract_practice_keys

        item.practice_keys = extract_practice_keys(referral.text)
        if self.directory is not None:
            apply_practice_lookup(item, self.directory)
        self.save(item)
        return item

    def save(self, item: ReviewItem) -> None:
        tmp = self._path(item.id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(item), f, indent=2, default=str)
        os.replace(tmp, self._path(item.id))     # atomic: never a half file

    def get(self, item_id: str) -> Optional[ReviewItem]:
        try:
            with open(self._path(item_id), encoding="utf-8") as f:
                return ReviewItem(**json.load(f))
        except (OSError, TypeError, json.JSONDecodeError):
            return None

    def all(self) -> list[ReviewItem]:
        items = []
        for name in os.listdir(self.root):
            if not name.endswith(".json"):
                continue
            item = self.get(name[:-5])
            if item:
                items.append(item)
        order = {NEEDS_REVIEW: 0, BLOCKED: 1, NEEDS_PAYER: 2, DONE: 3}
        return sorted(items, key=lambda i: (order.get(i.state, 9),
                                            i.received), reverse=False)

    def by_state(self, state: str) -> list[ReviewItem]:
        return [i for i in self.all() if i.state == state]


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

def apply_practice_lookup(item: ReviewItem, directory) -> None:
    """Fill or correct the practice name from the directory of practices
    confirmed on earlier referrals.

    Always confidence 'check', never 'ok': the match is deterministic, but
    the reviewer should still see that the name came from the directory
    rather than off this referral, and what it matched on. When the
    referral and the directory disagree, the directory wins — its name was
    typed or approved by a human, the referral's came out of OCR — but the
    note keeps what the referral said, so a genuine change of name at the
    same phone number is visible rather than silently papered over."""
    match = directory.lookup(item.practice_keys)
    if match is None:
        return
    n = match.confirmed
    times = (f"confirmed on {n} earlier referral{'s' if n != 1 else ''}"
             if n else "in the practice directory")
    extracted = item.fields.get("gp_practice")
    if not extracted:
        note = (f"not readable on this referral — recognised by "
                f"{match.matched_on}, {times}")
    elif _same_name(extracted, match.name):
        note = f"matches the practice directory ({times})"
    else:
        note = (f"the referral reads '{extracted}', but this practice — "
                f"recognised by {match.matched_on} — was {times} as "
                f"'{match.name}'; check it hasn't changed its name")
    item.fields["gp_practice"] = match.name
    item.confidence["gp_practice"] = "check"
    item.notes["gp_practice"] = note


def _same_name(a: str, b: str) -> bool:
    import re

    strip = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    return strip(a) == strip(b)


def _is_dry_run(body: Any) -> bool:
    return isinstance(body, dict) and bool(body.get("dry_run"))


def describe_case(case: dict) -> str:
    """Enough about an existing case to tell a repeat from a new referral.

    A patient referred again next year is the normal reason for a second
    CCMP case, so the deciding question is whether the previous plan is
    used up. The payer answers it: Sessions_Completed against
    Sessions_Approved, plus the date it was referred.
    """
    parts = [f"case {case.get('ID')}"]
    status = str(case.get("status") or "").strip()
    if status:
        parts.append(status.lower())
    for payer in case.get("payers") or []:
        if not isinstance(payer, dict):
            continue
        approved, completed = NookalClient.payer_sessions(payer)
        if approved is not None:
            parts.append(f"{completed if completed is not None else '?'} of "
                         f"{approved} sessions used")
        referred = str(payer.get("ReferralDate") or "").strip()
        if referred and not referred.startswith("0000"):
            parts.append(f"referred {referred}")
    return ", ".join(parts)


def existing_ccmp_cases(client: NookalClient, patient_id: int) -> list[dict]:
    """Cases already titled GP CCMP on this patient.

    A patient can legitimately be referred again — a new plan each year is
    normal — so this is not an error. But re-processing a referral that has
    already been done is far more common than a genuine second referral,
    and a duplicate case is tedious to unpick. The operator decides.

    getCases returns the title as `caseTitle`; addCase echoes it as `title`.
    """
    try:
        cases = client.get_cases(patient_id) or []
    except NookalError:
        return []          # never block a create because a lookup failed
    found = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        title = case.get("caseTitle") or case.get("title") or ""
        if str(title).strip() == NookalClient.CASE_TITLE:
            found.append(case)
    return found


def split_name(full: str) -> tuple[str, str]:
    """'Linda May Kolb' -> ('Linda May', 'Kolb'). The last word is the
    surname; everything before it is given names, which is what Nookal's
    two fields expect."""
    parts = [p for p in re.split(r"\s+", full.strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


# --------------------------------------------------------------------------
# The write sequence
# --------------------------------------------------------------------------

@dataclass
class CreateResult:
    ok: bool
    state: str
    message: str
    patient_id: Optional[int] = None
    case_id: Optional[int] = None
    file_id: Optional[str] = None
    payer_instructions: dict[str, Any] = _field(default_factory=dict)


def payer_instructions(item: ReviewItem) -> dict[str, Any]:
    """What the operator has to type into Add Payer, gathered in one place
    so the manual step is a copy-read rather than a hunt through the PDF."""
    sessions = item.fields.get("services_count") or DEFAULT_SESSIONS
    stated = bool(item.fields.get("services_count"))
    return {
        "payer_type": "Medicare",
        "sessions": sessions,
        "sessions_stated": stated,
        "sessions_warning": (
            f"Enter {sessions}. Never enter 0 — in Nookal that means "
            "Unlimited, which removes the cap altogether."
            if stated else
            f"Not stated on the referral, so use the standard "
            f"{DEFAULT_SESSIONS}. Never enter 0 — in Nookal that means "
            "Unlimited, which removes the cap altogether."),
        "referring_gp": item.fields.get("gp_name"),
        "provider_number": item.fields.get("gp_provider_number"),
        "referral_date": item.fields.get("referral_date"),
    }


def create_in_nookal(client: NookalClient, item: ReviewItem,
                     queue: Optional[Queue] = None) -> CreateResult:
    """Create the patient, Medicare details, case and document.

    Deliberately does NOT create the payer or book appointments — see
    FINDINGS.md. Stops without writing anything when the patient match is
    ambiguous: a duplicate record is worse than a pause.

    Every outcome is persisted, including the ones that refuse to act. A
    reviewer who clicks Create and gets silence has no idea what happened.
    """
    result = _create(client, item)
    if queue is not None:
        item.state = result.state
        item.message = result.message
        if result.ok:
            item.nookal = {"patient_id": result.patient_id,
                           "case_id": result.case_id,
                           "file_id": result.file_id,
                           "payer": result.payer_instructions}
        queue.save(item)
    return result


def _create(client: NookalClient, item: ReviewItem) -> CreateResult:
    missing = item.missing_required()
    if missing:
        return CreateResult(False, BLOCKED,
                            f"cannot create: {', '.join(missing)} is missing")

    first, last = split_name(item.fields["patient_name"])
    if not last:
        return CreateResult(False, BLOCKED,
                            "cannot create: patient name is unusable")

    try:
        patient, disposition = client.search_or_create_patient(
            first, last, item.fields["dob"])
    except NookalError as exc:
        return CreateResult(False, BLOCKED, f"patient lookup failed: {exc}")

    if disposition in ("ambiguous", "fuzzy_review"):
        explain = {
            "ambiguous": "several patients match this name and date of "
                         "birth — pick the right one in Nookal, then enter "
                         "their patient ID here",
            "fuzzy_review": "no exact match, but similar names exist — "
                            "confirm whether this is an existing patient "
                            "before a duplicate record is created",
        }[disposition]
        return CreateResult(False, BLOCKED, explain)

    # Ask the client, not the response: a dry run against an EXISTING
    # patient returns a real record, so inferring it from the payload
    # missed exactly the case that archived referrals produce.
    rehearsal = bool(getattr(client, "dry_run", False))
    patient_id = _patient_id(client, patient)
    if patient_id is None and rehearsal:
        patient_id = DRY_RUN_ID          # nothing was created to have an id
    if patient_id is None:
        return CreateResult(False, BLOCKED,
                            "patient created but no id came back — check "
                            "Nookal before retrying, to avoid a duplicate")

    # An existing GP CCMP case usually means this referral has already been
    # processed. Stop and let the operator confirm rather than quietly
    # creating a second one.
    if not item.allow_duplicate_case and not rehearsal:
        already = existing_ccmp_cases(client, patient_id)
        if already:
            described = "; ".join(describe_case(c) for c in already)
            return CreateResult(
                False, BLOCKED,
                f"this patient already has a {NookalClient.CASE_TITLE} case "
                f"— {described}. If that is this same referral, there is "
                "nothing to do. If the previous plan is used up and this is "
                "a new year's referral, use Create anyway.")

    result = CreateResult(True, NEEDS_PAYER, "", patient_id=patient_id)
    steps: list[str] = []
    if rehearsal:
        steps.append("DRY RUN — nothing was written to Nookal")
    steps.append(f"patient {disposition}"
                 + ("" if patient_id == DRY_RUN_ID else f" ({patient_id})"))

    # Medicare — only ever written when the check digit passes.
    number = item.fields.get("medicare_no")
    if number and medicare_number_valid(number):
        try:
            client.update_medicare(patient_id, number,
                                   item.fields.get("medicare_irn") or "1",
                                   item.fields.get("medicare_expiry"))
            steps.append("Medicare written")
        except NookalError as exc:
            steps.append(f"Medicare NOT written ({exc})")
    elif number:
        steps.append("Medicare NOT written — fails check digit")

    try:
        case = client.add_case(
            patient_id,
            referral_date=item.fields.get("referral_date"),
            notes=format_case_notes(_notes_payload(item)))
        result.case_id = (DRY_RUN_ID if _is_dry_run(case)
                          else _case_id(client, case))
        steps.append("case created"
                     + ("" if result.case_id == DRY_RUN_ID
                        else f" ({result.case_id})"))
    except NookalError as exc:
        result.ok = False
        result.state = BLOCKED
        result.message = f"case creation failed: {exc}"
        return result

    try:
        with open(item.pdf_path, "rb") as f:
            upload = client.upload_pdf(
                patient_id, f.read(),
                _document_name(item), case_id=result.case_id)
        result.file_id = upload.get("file_id")
        steps.append("referral PDF attached")
    except (NookalError, OSError) as exc:
        steps.append(f"PDF NOT attached ({exc}) — add it by hand")

    result.message = "; ".join(steps)
    result.payer_instructions = payer_instructions(item)
    return result


@dataclass
class PayerCheck:
    ok: bool
    message: str
    approved: Optional[int] = None
    completed: Optional[int] = None


def verify_payer(client: NookalClient, item: ReviewItem) -> PayerCheck:
    """Read the payer back after the operator says they added it.

    The payer cannot be written through the API, but it CAN be read, so the
    one manual step in the pipeline still gets checked. This catches the
    failure that is otherwise invisible: Sessions = 0, which Nookal treats
    as Unlimited rather than as zero.
    """
    patient_id = item.nookal.get("patient_id")
    case_id = str(item.nookal.get("case_id") or "")
    if not patient_id or not case_id:
        return PayerCheck(False, "no Nookal case recorded for this referral")

    try:
        cases = client.get_cases(int(patient_id)) or []
    except (NookalError, TypeError, ValueError) as exc:
        return PayerCheck(False, f"could not read the case back: {exc}")

    payers: list = []
    for case in cases:
        if isinstance(case, dict) and str(case.get("ID")) == case_id:
            payers = [p for p in (case.get("payers") or [])
                      if isinstance(p, dict)]
            break
    if not payers:
        return PayerCheck(False, f"no payer on case {case_id} yet — add it "
                                 "in Nookal (Case -> Add Payer -> Medicare) "
                                 "before marking this done")

    payer = payers[0]
    approved, completed = client.payer_sessions(payer)
    expected = item.fields.get("services_count") or DEFAULT_SESSIONS

    if approved == 0:
        return PayerCheck(False, "the payer says 0 sessions, which in Nookal "
                                 "means UNLIMITED, not none. Open the case "
                                 f"and set it to {expected}.",
                          approved, completed)
    if approved is None:
        return PayerCheck(False, "could not read a session count off the "
                                 "payer — check it by hand.", None, completed)
    if approved != expected:
        return PayerCheck(False, f"the payer says {approved} sessions but "
                                 f"this referral expects {expected}. Fix "
                                 "whichever is wrong.", approved, completed)
    return PayerCheck(True, f"payer confirmed: {approved} sessions approved, "
                            f"{completed} used.", approved, completed)


def _notes_payload(item: ReviewItem) -> dict:
    payload = dict(item.fields)
    payload["received_date"] = item.received[:10]
    # Mark the session count for verification whenever it was not stated,
    # so the case notes say so rather than implying a confirmed number.
    payload["services_flagged"] = not item.fields.get("services_count")
    return payload


def _document_name(item: ReviewItem) -> str:
    date = item.fields.get("referral_date") or item.received[:10]
    return f"GP CCMP referral {date}"


def _patient_id(client: NookalClient, patient: Any) -> Optional[int]:
    if isinstance(patient, dict):
        for key in ("patient_id", "ID", "Id", "id", "patientID"):
            if key in patient:
                try:
                    return int(patient[key])
                except (TypeError, ValueError):
                    return None
    return None


def _case_id(client: NookalClient, body: Any) -> Optional[int]:
    value = client.extract(body, "cases", "case", "results")
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        for key in ("ID", "Id", "id", "case_id", "caseID"):
            if key in value:
                try:
                    return int(value[key])
                except (TypeError, ValueError):
                    return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
