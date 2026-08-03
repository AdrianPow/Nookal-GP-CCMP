#!/usr/bin/env python3
"""
diagnostic.py — one-shot probe of the Nookal API against your live account.

Answers, in priority order, every question the printed docs left open:

  0  Does the API key authenticate, and does POST or GET work?
  1  Reference data: location / practitioner / service IDs (and whether
     service_id == appointment_type_id).
  2  addPatient: is 'phone' accepted, or only home/mobile/work?
     Creates the throwaway patient ZZTEST APIDIAG that later phases use.
  3  addCase titled 'GP CCMP': does it match the existing dropdown option
     or create a duplicate? What does a NOVEL title do? (You check the
     dropdown in the UI afterwards — the API can't see the list.)
  4  updateMedicareDetails: what does the Health tab show for a Y-m-d
     expiry (month/year only in the UI)?
  5  editCasePayer: what does payer_id actually reference? Probed with a
     nonsense id to read the error, then optionally against a real payer
     you add in the UI.
  6  getCases/getAllCases: are referrer/payer contact IDs exposed
     (harvestable for a GP lookup table)?
  7  getServiceRedemptions: what does it return for a patient with an
     active CCMP? (--redemptions-patient-id)
  8  uploadFile → S3 PUT → activateFile: which order works, and does the
     file appear under Documents?
  9  getAppointments: are appt_status / service_id filters accepted?

Everything is written to diagnostic_report.txt (raw JSON included).
Writes touch ONLY the ZZTEST patient. Delete that patient in the UI when
you're done, plus any 'ZZ DIAGNOSTIC' dropdown title phase 3 created.

Usage:
    pip install requests
    cp config.example.json nookal_config.json   # then paste your API key
    python3 diagnostic.py --dry-run --yes       # rehearsal: reads only,
                                                # every write is a no-op
    python3 diagnostic.py                       # all phases, prompts first
    python3 diagnostic.py --yes                 # no prompts
    python3 diagnostic.py --phases 0,1,2
    python3 diagnostic.py --patient-id 999      # reuse existing ZZTEST
    python3 diagnostic.py --redemptions-patient-id 713
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from nookal_client import (NookalClient, NookalConfig, NookalError,
                           NookalAPIError, UnknownMethodError,
                           medicare_number_valid, provider_number_valid)

REPORT_PATH = "diagnostic_report.txt"

# Checksum-valid fake Medicare number (passes the digit-9 rule; card issue 1)
FAKE_MEDICARE = "2123456701"
FAKE_IRN = "1"
FAKE_MEDICARE_EXPIRY = "2027-11-30"   # UI shows month/year only — phase 4
                                      # asks you to check what displays.

TEST_FIRST, TEST_LAST, TEST_DOB = "ZZTEST", "APIDIAG", "1990-01-01"

# Stand-in patient id used under --dry-run, where no patient is created but
# the later phases should still show what they would call.
DRY_RUN_PID = 0

# A structurally minimal but valid one-page PDF, so phase 8 needs no deps.
MINIMAL_PDF = (
    b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
    b"0000000052 00000 n \n0000000101 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n164\n%%EOF\n"
)


class Report:
    def __init__(self, path: str):
        self.f = open(path, "w", encoding="utf-8")
        self.say(f"Nookal API diagnostic — {dt.datetime.now():%Y-%m-%d %H:%M}")
        self.say("=" * 70)

    def say(self, *lines: str) -> None:
        for line in lines:
            print(line)
            self.f.write(line + "\n")
        self.f.flush()

    def raw(self, label: str, obj) -> None:
        text = json.dumps(obj, indent=2, default=str)
        if len(text) > 4000:
            text = text[:4000] + "\n... [truncated in report]"
        self.say(f"--- raw: {label} ---", text, "")

    def finding(self, text: str) -> None:
        self.say(f"  >> FINDING: {text}")

    def action(self, text: str) -> None:
        self.say(f"  ** YOU CHECK: {text}")


def confirm(prompt: str, auto_yes: bool) -> bool:
    if auto_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


def is_dry(body) -> bool:
    """True when a write was suppressed by --dry-run.

    Guards every 'the API accepted X' finding: under a rehearsal the call
    never left the machine, so reporting it as confirmed would be a lie in
    the report the whole project keys off.
    """
    return isinstance(body, dict) and bool(body.get("dry_run"))


def get_id(client: NookalClient, body, *keys) -> int | None:
    val = client.extract(body, *keys)
    if isinstance(val, list) and val:
        val = val[0]
    if isinstance(val, dict):
        for k in ("ID", "Id", "id", "patient_id", "case_id", "patientID",
                  "caseID"):
            if k in val:
                val = val[k]
                break
    try:
        return int(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- phases

def phase0_auth(client: NookalClient, rep: Report) -> bool:
    rep.say("", "PHASE 0 — authentication & transport")
    for method in (client.config.http_method, 
                   "GET" if client.config.http_method == "POST" else "POST"):
        client.config.http_method = method
        try:
            body = client.verify()
            rep.raw(f"verify via {method}", body)
            rep.finding(f"API key authenticates using HTTP {method}. "
                        f"Set http_method='{method}' in nookal_config.json.")
            return True
        except NookalError as exc:
            rep.say(f"  verify via {method} failed: {exc}")
    rep.finding("Authentication failed with both POST and GET — check the "
                "key, or Nookal may want a different auth parameter. Stop "
                "here and inspect the raw responses above.")
    return False


def phase1_reference(client: NookalClient, rep: Report) -> dict:
    rep.say("", "PHASE 1 — reference data")
    out: dict = {}
    for label, fn in (("locations", client.locations),
                      ("practitioners", client.practitioners),
                      ("services", client.services)):
        try:
            items = fn()
            out[label] = items
            rep.raw(label, items)
        except NookalError as exc:
            rep.say(f"  {label} FAILED: {exc}")
    svc = out.get("services") or []
    if svc and isinstance(svc, list) and isinstance(svc[0], dict):
        keys = set(svc[0].keys())
        rep.finding(f"service record keys: {sorted(keys)}")
        rep.finding("Compare the id field name here against what "
                    "getAppointments' service_id filter expects (phase 9) — "
                    "this settles service_id vs appointment_type_id.")
    return out


def phase2_patient(client: NookalClient, rep: Report, auto_yes: bool,
                   existing_id: int | None) -> int | None:
    rep.say("", "PHASE 2 — addPatient field semantics + throwaway patient")
    if existing_id:
        rep.say(f"  reusing patient_id {existing_id} (--patient-id)")
        return existing_id
    if not confirm(f"Create throwaway patient {TEST_FIRST} {TEST_LAST}?",
                   auto_yes):
        return None
    # 2a: mobile (per the field table)
    try:
        body = client.add_patient(TEST_FIRST, TEST_LAST, TEST_DOB,
                                  mobile="0400000001",
                                  email="apidiag@example.invalid",
                                  address_line_1="1 Test St", city="Brisbane",
                                  state="QLD", postcode="4000",
                                  country="Australia")
        rep.raw("addPatient (mobile=...)", body)
        if is_dry(body):
            rep.finding(f"DRY RUN — no patient created. Using placeholder "
                        f"patient_id {DRY_RUN_PID} so the remaining phases "
                        "still print their call sequence; reads against it "
                        "will come back empty, which is expected.")
            return DRY_RUN_PID
        pid = get_id(client, body, "patient", "patients", "results", "data")
        rep.finding(f"addPatient accepted home/mobile/work-style fields; "
                    f"patient_id = {pid}")
    except NookalError as exc:
        rep.say(f"  addPatient with mobile failed: {exc}")
        rep.raw("failure payload", getattr(exc, "payload", None))
        pid = None
    # 2b: does 'phone' (the docs example) work as an edit?
    if pid:
        try:
            body = client.edit_patient(pid, phone="0400000002")
            rep.raw("editPatient (phone=...)", body)
            rep.finding("'phone' parameter was accepted — docs example is "
                        "valid. Check in the UI which slot it landed in.")
        except NookalError as exc:
            rep.finding(f"'phone' rejected ({exc}) — use home/mobile/work "
                        "only, the docs example is wrong.")
    return pid


def phase3_case_title(client: NookalClient, rep: Report, auto_yes: bool,
                      pid: int) -> int | None:
    rep.say("", "PHASE 3 — case title vs the managed dropdown  [HIGHEST "
            "PRIORITY]")
    case_id = None
    if confirm("Create case titled 'GP CCMP' on the test patient?",
               auto_yes):
        try:
            body = client.add_case(pid, referral_date="2025-06-18",
                                   notes="ZZ DIAGNOSTIC case — safe to "
                                         "delete")
            rep.raw("addCase title='GP CCMP'", body)
            if is_dry(body):
                rep.finding("DRY RUN — would create a case titled "
                            f"'{client.CASE_TITLE}'. Rerun without --dry-run "
                            "to settle the dropdown question.")
                return None
            case_id = get_id(client, body, "case", "cases", "results",
                             "data")
            rep.finding(f"case created, case_id = {case_id}")
            rep.action("Open the patient's Cases tab: does this case show "
                       "title 'GP CCMP' matching the EXISTING dropdown "
                       "option (open the dropdown on another case — is "
                       "'GP CCMP' listed once, or twice)?")
        except NookalError as exc:
            rep.say(f"  addCase failed: {exc}")
            rep.raw("failure payload", getattr(exc, "payload", None))
    if confirm("Also test a NOVEL title (may add junk to the dropdown — "
               "you'll need to delete it in the UI)?", auto_yes):
        client.allow_any_title()
        try:
            body = client.add_case(pid, title="ZZ DIAGNOSTIC TITLE TEST",
                                   notes="delete me")
            rep.raw("addCase novel title", body)
            rep.action("Check the Title dropdown clinic-wide (Manage/Setup "
                       "or any case): did 'ZZ DIAGNOSTIC TITLE TEST' get "
                       "ADDED to the list, is it free text on this case "
                       "only, or was it silently replaced? This decides "
                       "whether automated case creation is safe. Delete the "
                       "junk option if it was added.")
        except NookalError as exc:
            rep.finding(f"novel title REJECTED ({exc}) — the API enforces "
                        "the managed list. Excellent: automation cannot "
                        "pollute the dropdown.")
    return case_id


def phase4_medicare(client: NookalClient, rep: Report, auto_yes: bool,
                    pid: int) -> None:
    rep.say("", "PHASE 4 — Medicare details & expiry format")
    if not medicare_number_valid(FAKE_MEDICARE):
        rep.say(f"  SKIPPED: the fake number {FAKE_MEDICARE} no longer "
                "passes check-digit validation — the client would refuse "
                "to write it anyway.")
        return
    if not confirm("Write fake Medicare details to the test patient?",
                   auto_yes):
        return
    try:
        body = client.update_medicare(pid, FAKE_MEDICARE, FAKE_IRN,
                                      FAKE_MEDICARE_EXPIRY)
        rep.raw("updateMedicareDetails", body)
        if is_dry(body):
            rep.finding("DRY RUN — Medicare details not written, so there "
                        "is nothing to check in the Health tab yet.")
            return
        rep.action(f"Open Health tab for {TEST_FIRST} {TEST_LAST}: number "
                   f"{FAKE_MEDICARE}, IRN {FAKE_IRN}, and what does the "
                   f"expiry show for {FAKE_MEDICARE_EXPIRY} (month/year "
                   "correct)? Is the 'Unverified' toggle set?")
    except NookalError as exc:
        rep.say(f"  updateMedicareDetails failed: {exc}")
        rep.raw("failure payload", getattr(exc, "payload", None))


def payers_of(client: NookalClient, pid: int) -> dict:
    """Every payer currently attached to this patient's cases, keyed by
    case id — used to see whether a write actually changed anything."""
    out: dict = {}
    for case in client.get_cases(pid) or []:
        if isinstance(case, dict):
            out[str(case.get("ID"))] = case.get("payers")
    return out


def phase5_real_payer(client: NookalClient, rep: Report, auto_yes: bool,
                      pid: int, payer_id: int, sessions: int | None) -> None:
    """The test phase 5 always needed: edit a payer that actually exists,
    then read it back to see whether anything moved.

    The original probe used a nonsense payer_id against a case with no
    payers at all, so 'success, nothing changed' was the expected result of
    an UPDATE matching zero rows — not evidence the endpoint is broken.
    """
    rep.say("", "PHASE 5 — editCasePayer against a REAL payer  [WRITE]")
    before = payers_of(client, pid)
    rep.raw("payers BEFORE", before)
    if not any(before.values()):
        rep.say("  STOP: this patient has no payer on any case. Add one in "
                "the UI first (Case -> Add Payer -> Medicare -> Sessions), "
                "then rerun with --payer-id.")
        return

    fields: dict = {"reference": f"ZZ DIAG {dt.datetime.now():%H%M%S}"}
    if sessions is not None:
        fields["sessions"] = sessions
    rep.say(f"  sending editCasePayer payer_id={payer_id} {fields}")
    if not confirm("This WRITES to a real case payer. Continue?", auto_yes):
        return
    try:
        body = client.edit_case_payer(pid, payer_id, **fields)
        rep.raw("editCasePayer response", body)
        if is_dry(body):
            rep.finding("DRY RUN — nothing sent.")
            return
    except NookalError as exc:
        rep.finding(f"editCasePayer REJECTED it: {exc}")
        rep.raw("failure payload", getattr(exc, "payload", None))
        return

    after = payers_of(client, pid)
    rep.raw("payers AFTER", after)
    if after != before:
        rep.finding("THE PAYER CHANGED — editCasePayer does work against a "
                    "real payer id. Compare the two payer blocks above to "
                    "see which fields it accepted; that decides how much of "
                    "the payer step can be automated.")
    else:
        rep.finding("Response said success but the payer is byte-identical "
                    "afterwards. Either the field names are wrong (compare "
                    "them against the payer keys above) or the endpoint is "
                    "a no-op. Payer creation stays manual.")
    rep.action("Open the case in Nookal and confirm the screen agrees with "
               "the 'payers AFTER' block — especially the session count.")


def phase5_payer(client: NookalClient, rep: Report, auto_yes: bool,
                 pid: int) -> None:
    rep.say("", "PHASE 5 — editCasePayer semantics")
    rep.say("  NOTE: this probe uses a payer_id that does not exist, so a "
            "'success' here means nothing — an UPDATE matching zero rows "
            "reports success too. Use --payer-id for the real test.")
    if confirm("Probe editCasePayer with a nonsense payer_id (reads the "
               "error message)?", auto_yes):
        try:
            body = client.edit_case_payer(pid, 999999,
                                          reference="ZZ DIAG probe")
            if is_dry(body):
                rep.finding("DRY RUN — editCasePayer not sent, so the error "
                            "message that settles payer_id semantics was "
                            "never returned.")
                return
            rep.raw("editCasePayer payer_id=999999 (accepted)", body)
            rep.finding("A nonsense payer_id was ACCEPTED — so the endpoint "
                        "does not validate the id, and this tells us "
                        "nothing about whether it works. Rerun with "
                        "--payer-id of a real payer to find out.")
        except NookalError as exc:
            rep.finding(f"error text for nonsense payer_id: {exc}")
            rep.raw("failure payload", getattr(exc, "payload", None))
            rep.say("  Read that message: 'payer not found' implies "
                    "payer_id = an existing case-payer link (UI-first, "
                    "manual Add Payer stays); 'invalid payer type' would "
                    "imply it can attach payers.")
    rep.action("For the definitive answer: add a Medicare payer to the "
               "diagnostic case in the UI (Add Payer → Medicare → Sessions "
               "→ save), then rerun --phases 6 and look for a payer id in "
               "getCases; then rerun 5 with that id to see if reference/"
               "expiry/referrer_id write through.")


def phase6_case_shape(client: NookalClient, rep: Report, pid: int) -> None:
    rep.say("", "PHASE 6 — case object shape (referrer/payer harvesting)")
    try:
        cases = client.get_cases(pid)
        rep.raw(f"getCases patient {pid}", cases)
        if cases and isinstance(cases[0], dict):
            rep.finding(f"case record keys: {sorted(cases[0].keys())}")
        # Surface any payer ids plainly — phase 5's real test needs one, and
        # digging it out of the raw JSON above is needless work.
        found_payer = False
        for case in cases or []:
            if not isinstance(case, dict):
                continue
            for payer in case.get("payers") or []:
                found_payer = True
                ids = {k: v for k, v in payer.items()
                       if isinstance(k, str) and "id" in k.lower()} \
                    if isinstance(payer, dict) else {}
                rep.finding(f"case {case.get('ID')} has a payer: "
                            f"ids={ids or payer}")
                if isinstance(payer, dict):
                    rep.finding(f"  payer keys: {sorted(payer.keys())}")
        if not found_payer:
            rep.say("  No payer on any case. To test whether payers can be "
                    "automated, add one in the UI (Case -> Add Payer -> "
                    "Medicare -> Sessions), then rerun this phase.")
        else:
            rep.action("Take the payer id above and run: "
                       f"--phases 5 --patient-id {pid} --payer-id <that id>")
    except NookalError as exc:
        rep.say(f"  getCases failed: {exc}")
    try:
        body = client.call("getAllCases", page=1, page_length=5)
        rep.raw("getAllCases page 1 (first 5)", body)
        rep.say("  Look for referrer / contact / payer id fields in the "
                "records above — if present, GP contact IDs can be "
                "harvested from historical cases into a lookup table.")
    except NookalError as exc:
        rep.say(f"  getAllCases failed: {exc}")


def phase7_redemptions(client: NookalClient, rep: Report,
                       redemption_pid: int | None) -> None:
    rep.say("", "PHASE 7 — getServiceRedemptions")
    if not redemption_pid:
        rep.say("  skipped: pass --redemptions-patient-id <id> of a REAL "
                "patient with an active CCMP (read-only, safe).")
        return
    try:
        body = client.get_service_redemptions(redemption_pid)
        rep.raw(f"getServiceRedemptions patient {redemption_pid}", body)
        rep.say("  If this reflects Medicare allocations it beats counting "
                "appointments; if it's pre-paid class packs, ignore it.")
    except NookalError as exc:
        rep.say(f"  getServiceRedemptions failed: {exc}")


def describe_upload(result, docs) -> str | None:
    """Read the uploaded file's own record back and say plainly whether it
    landed. CONFIRMED live 2026-08-03: status "1" is active and visible in
    the Documents tab; status "2" is registered but invisible, which is what
    an upload that never got activated looks like."""
    if not isinstance(result, dict) or not isinstance(docs, list):
        return None
    file_id = result.get("file_id")
    match = next((d for d in docs if isinstance(d, dict)
                  and d.get("ID") == file_id), None)
    if match is None:
        return (f"the uploaded file_id {file_id!r} is not in the document "
                "list at all — the upload did not stick.")
    status = str(match.get("status"))
    case_id = match.get("caseID")
    visible = ("ACTIVE and visible in the Documents tab" if status == "1"
               else f"status {status} — registered but NOT visible in the UI")
    attached = (f"attached to case {case_id}" if case_id
                else "NOT attached to any case (it will sit at the top level "
                     "of Documents rather than in the case folder)")
    return f"uploaded file is {visible}, and {attached}."


def phase8_upload(client: NookalClient, rep: Report, auto_yes: bool,
                  pid: int, case_id: int | None) -> None:
    rep.say("", "PHASE 8 — three-step file upload")
    if not confirm("Upload a tiny test PDF to the test patient?", auto_yes):
        return
    if case_id is None:
        rep.say("  NOTE: no case id available, so the upload will not be "
                "attached to a case. Pass --case-id to test attachment.")
    result: dict = {}
    try:
        result = client.upload_pdf(pid, MINIMAL_PDF, "ZZ_DIAG_upload",
                                   case_id=case_id)
        rep.raw("upload_pdf (PUT then activate)", result)
        if is_dry(result.get("register")):
            rep.finding("DRY RUN — nothing registered or uploaded; the "
                        "activate-order question is still open.")
        else:
            rep.finding(f"PUT status {result.get('put_status')}; activate "
                        "response above. If activate succeeded AFTER the "
                        "PUT, the docs' note is inverted as suspected.")
    except NookalError as exc:
        rep.say(f"  upload (PUT-then-activate) failed: {exc}")
        if confirm("Try the other order (activate BEFORE PUT)?", auto_yes):
            try:
                result = client.upload_pdf(pid, MINIMAL_PDF,
                                           "ZZ_DIAG_upload2",
                                           case_id=case_id,
                                           activate_before_put=True)
                rep.raw("upload_pdf (activate then PUT)", result)
                rep.finding("activate-before-PUT order worked — docs note "
                            "was literal after all.")
            except NookalError as exc2:
                rep.say(f"  activate-before-PUT also failed: {exc2}")
    try:
        docs = client.get_patient_documents(pid)
        rep.raw("getPatientDocuments", docs)
        uploaded = describe_upload(result, docs)
        if uploaded:
            rep.finding(uploaded)
        rep.action("Open Documents on the test patient: does ZZ_DIAG_upload "
                   "appear, does it open, and is it inside the case folder?")
    except NookalError as exc:
        rep.say(f"  getPatientDocuments failed: {exc}")


def appointment_count(client: NookalClient, body) -> int:
    items = client.extract(body, "appointments", "Appointments", "results")
    return len(items) if isinstance(items, list) else 0


INCONCLUSIVE = (
    "but the patient has no appointments, so this proves nothing — a "
    "filter that is silently ignored also returns an empty list. Re-run "
    "with --patient-id of a real patient who has appointment history."
)


def phase9_appointments(client: NookalClient, rep: Report,
                        pid: int) -> None:
    rep.say("", "PHASE 9 — getAppointments filters")
    try:
        body = client.call("getAppointments", patient_id=pid,
                           date_from="2025-01-01",
                           appt_status="Completed", page=1, page_length=10)
        rep.raw("getAppointments (status filter)", body)
        found = appointment_count(client, body)
        if found:
            rep.finding(f"appt_status filter accepted and returned {found} "
                        "completed appointment(s) — the filter is real.")
        else:
            rep.finding(f"appt_status filter was accepted, {INCONCLUSIVE}")
    except NookalError as exc:
        rep.say(f"  getAppointments failed: {exc}")
    svc = client._cache.get("services") or []
    if svc and isinstance(svc[0], dict):
        sid = None
        for k in ("ID", "Id", "id", "service_id", "appointment_type_id"):
            if k in svc[0]:
                sid = svc[0][k]
                break
        if sid is not None:
            try:
                body = client.call("getAppointments", patient_id=pid,
                                   date_from="2025-01-01", service_id=sid,
                                   page=1, page_length=5)
                rep.raw(f"getAppointments service_id={sid}", body)
                found = appointment_count(client, body)
                if found:
                    rep.finding(f"service_id={sid} from getServices returned "
                                f"{found} appointment(s) — service_id and "
                                "appointment_type_id are interchangeable.")
                else:
                    rep.finding(f"service_id={sid} was accepted, "
                                f"{INCONCLUSIVE}")
            except NookalError as exc:
                rep.finding(f"service_id={sid} rejected: {exc} — the two "
                            "id spaces differ; map them via getServices "
                            "before filtering.")


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--yes", action="store_true",
                    help="run all phases without prompting")
    ap.add_argument("--phases", default="all",
                    help="comma-separated phase numbers, e.g. 0,1,2")
    ap.add_argument("--patient-id", type=int, default=None,
                    help="reuse an existing ZZTEST patient id")
    ap.add_argument("--case-id", type=int, default=None,
                    help="attach phase 8's upload to an existing case id "
                         "(without it the file lands outside any case)")
    ap.add_argument("--payer-id", type=int, default=None,
                    help="phase 5: edit this REAL payer and read it back, "
                         "instead of probing a nonsense id")
    ap.add_argument("--payer-sessions", type=int, default=None,
                    help="phase 5: session count to try writing to that "
                         "payer (omit to only touch the reference field)")
    ap.add_argument("--redemptions-patient-id", type=int, default=None,
                    help="real patient id with an active CCMP for phase 7")
    ap.add_argument("--config", default="nookal_config.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="rehearsal: every WRITE becomes a logged no-op, so "
                         "nothing is created in the clinic. Read calls still "
                         "go out, so auth, endpoint names and reference data "
                         "are confirmed for real.")
    args = ap.parse_args()

    wanted = (set(range(10)) if args.phases == "all"
              else {int(p) for p in args.phases.split(",")})

    rep = Report(REPORT_PATH)
    if args.dry_run:
        rep.say("*** DRY RUN — writes are logged, not sent. Reads still hit "
                "the live API. ***")
    try:
        client = NookalClient(NookalConfig.load(args.config),
                              dry_run=args.dry_run, verbose=True)
    except NookalError as exc:
        rep.say(f"CONFIG ERROR: {exc}")
        return 1

    # The validators must still agree with the sample referrals before we
    # write anything. Checked rather than asserted: `python -O` strips
    # asserts, and this is the guard that must never be optimised away.
    for ok, label in ((medicare_number_valid("4081334278"), "Medicare "
                       "4081334278"),
                      (provider_number_valid("0138434F"), "provider "
                       "0138434F"),
                      (provider_number_valid("228981BX"), "provider "
                       "228981BX")):
        if not ok:
            rep.say(f"VALIDATOR REGRESSION: sample {label} no longer "
                    "validates. Refusing to run — fix nookal_client.py "
                    "first.")
            return 1

    if 0 in wanted and not phase0_auth(client, rep):
        return 1
    if 1 in wanted:
        phase1_reference(client, rep)

    pid = args.patient_id
    if 2 in wanted:
        pid = phase2_patient(client, rep, args.yes, pid)
    if pid is None and wanted & {3, 4, 5, 6, 8, 9}:
        rep.say("", "No test patient available — phases 3-6 and 8-9 "
                "skipped. Rerun with --patient-id or allow phase 2.")
    case_id = args.case_id
    if pid is not None:
        if 3 in wanted:
            # Keep an explicitly passed --case-id if phase 3 makes none.
            case_id = phase3_case_title(client, rep, args.yes, pid) or case_id
        if 4 in wanted:
            phase4_medicare(client, rep, args.yes, pid)
        if 5 in wanted:
            if args.payer_id:
                phase5_real_payer(client, rep, args.yes, pid, args.payer_id,
                                  args.payer_sessions)
            else:
                phase5_payer(client, rep, args.yes, pid)
        if 6 in wanted:
            phase6_case_shape(client, rep, pid)
    if 7 in wanted:
        phase7_redemptions(client, rep, args.redemptions_patient_id)
    if pid is not None:
        if 8 in wanted:
            phase8_upload(client, rep, args.yes, pid, case_id)
        if 9 in wanted:
            phase9_appointments(client, rep, pid)

    rep.say("", "=" * 70,
            f"Done. Full detail in {REPORT_PATH}.",
            "Resolved endpoint names this session: "
            + json.dumps(client._resolved),
            "CLEAN-UP: delete the ZZTEST APIDIAG patient in the UI, and "
            "remove 'ZZ DIAGNOSTIC TITLE TEST' from the Title dropdown if "
            "phase 3 added it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
