"""
nookal_client.py — Python client for the Nookal practice-management REST API.

Built for the CCMP referral automation project (Embrace Movement Clinic).

Design notes
------------
* Endpoint names in Nookal's docs are truncated in the printed reference, so
  every method is called through a CANDIDATES registry: if the primary name is
  rejected as unknown, alternates are tried and the working name is cached.
  Run diagnostic.py once to confirm all names against the live API.
* All write calls are recorded to a JSONL audit log (config: audit_log).
* dry_run=True turns every write into a log entry + echo, no API call.
* This library deliberately does NOT create case payers or book appointments.
  Payers carry the session cap ("Zero = Unlimited" — a silent-failure trap)
  and appointments cannot be linked to a case via the API, so both stay in
  the Nookal UI by design.

Config file (JSON), default ./nookal_config.json:
    {
      "api_key":   "YOUR-KEY-HERE",
      "base_url":  "https://api.nookal.com/production/v2",
      "audit_log": "nookal_audit.jsonl",
      "http_method": "POST"          // "POST" (form-encoded) or "GET"
    }
The key can also come from the NOOKAL_API_KEY environment variable.
"""

from __future__ import annotations

import json
import os
import time
import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

try:
    import requests
except ImportError:  # allow validators to be imported/tested without requests
    requests = None  # type: ignore


# --------------------------------------------------------------------------
# Validators (no network needed; unit-tested against the sample referrals)
# --------------------------------------------------------------------------

def medicare_number_valid(number: str) -> bool:
    """Check-digit validation for a 10-digit Australian Medicare card number.

    Digit 1 must be 2-6; digits 1-8 weighted (1,3,7,9,1,3,7,9); the sum
    mod 10 must equal digit 9. Digit 10 is the card issue number (not
    checked). Verified against all three sample referrals.
    """
    digits = "".join(c for c in str(number) if c.isdigit())
    if len(digits) != 10:
        return False
    if digits[0] not in "23456":
        return False
    weights = (1, 3, 7, 9, 1, 3, 7, 9)
    total = sum(int(d) * w for d, w in zip(digits[:8], weights))
    return total % 10 == int(digits[8])


_PROVIDER_CHECK_ALPHABET = "YXWTLKJHFBA"
_PROVIDER_PLV_ALPHABET = "0123456789ABCDEF"  # practice-location values 0-15


def provider_number_valid(number: str) -> bool:
    """Check-character validation for an Australian Medicare provider number.

    Format: 6-digit stem + practice-location char + check char.
    check = (d1*3 + d2*5 + d3*8 + d4*4 + d5*2 + d6*1 + PLV*6) mod 11
    indexed into "YXWTLKJHFBA".
    Verified against both sample referrals (0138434F ✓, 228981BX ✓).
    Provider numbers shorter than 8 chars are left-padded with zeros on the
    stem (some sources print 5-digit stems).
    """
    s = str(number).strip().upper().replace(" ", "")
    if len(s) < 3:
        return False
    stem, plv_char, check_char = s[:-2], s[-2], s[-1]
    if not stem.isdigit():
        return False
    stem = stem.zfill(6)
    if len(stem) != 6:
        return False
    if plv_char not in _PROVIDER_PLV_ALPHABET:
        return False
    plv = _PROVIDER_PLV_ALPHABET.index(plv_char)
    weights = (3, 5, 8, 4, 2, 1)
    total = sum(int(d) * w for d, w in zip(stem, weights)) + plv * 6
    return _PROVIDER_CHECK_ALPHABET[total % 11] == check_char


def plausible_dob(dob: str, min_age: int = 0, max_age: int = 120) -> bool:
    """dob as YYYY-MM-DD; parseable, not in the future, within age bounds."""
    try:
        d = _dt.date.fromisoformat(dob)
    except (ValueError, TypeError):
        return False
    today = _dt.date.today()
    if d > today:
        return False
    age = (today - d).days / 365.25
    return min_age <= age <= max_age


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class NookalError(Exception):
    """Base class for client errors."""


class NookalAPIError(NookalError):
    """Nookal returned a failure status or unusable payload."""

    def __init__(self, method: str, message: str, payload: Any = None,
                 http_status: Optional[int] = None):
        self.method = method
        self.payload = payload
        self.http_status = http_status
        super().__init__(f"{method}: {message}")


class UnknownMethodError(NookalAPIError):
    """The endpoint name was not recognised by the API (used for fallback)."""


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass
class NookalConfig:
    api_key: str
    base_url: str = "https://api.nookal.com/production/v2"
    audit_log: str = "nookal_audit.jsonl"
    http_method: str = "POST"          # "POST" form-encoded, or "GET" query
    timeout: int = 30
    max_retries: int = 3               # transient (network / 5xx) retries

    @classmethod
    def load(cls, path: str = "nookal_config.json") -> "NookalConfig":
        data: dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        env_key = os.environ.get("NOOKAL_API_KEY")
        if env_key:
            data["api_key"] = env_key
        if not data.get("api_key"):
            raise NookalError(
                f"No API key: create {path} (see config.example.json) or set "
                "NOOKAL_API_KEY."
            )
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

# Docs headings -> likely endpoint names. First entry is tried first; on an
# "unknown function" style rejection the next is tried. Resolved names are
# cached per client instance for the session.
CANDIDATES: dict[str, list[str]] = {
    "verify":                ["verify"],
    "version":               ["version"],
    "getLocations":          ["getLocations"],
    "getPractitioners":      ["getPractitioners"],
    # CONFIRMED live 2026-08-03: getServices does not exist; the endpoint is
    # getAppointmentTypes, and its ServiceCode field carries the Medicare
    # item number (e.g. "10953" on Medicare EP (BB)).
    "getServices":           ["getAppointmentTypes", "getServices"],
    "getPatients":           ["getPatients"],
    "searchPatients":        ["searchPatients", "searchPatient"],
    "addPatient":            ["addPatient"],
    "editPatient":           ["editPatient", "updatePatient"],
    # CONFIRMED live 2026-08-03 by probe_endpoints.py: the documented
    # updateMedicareDetails does not exist. The real name is
    # updatePatientMedicareDetails — it was the only one of ten candidates
    # to answer with a validation error rather than the 404 page.
    "updateMedicareDetails": ["updatePatientMedicareDetails",
                              "updateMedicareDetails"],
    "updateDVADetails":      ["updateDVADetails"],
    "getCases":              ["getCases", "getPatientCases"],
    "getAllCases":           ["getAllCases"],
    "addCase":               ["addCase"],
    "updateCase":            ["updateCase"],
    "editCasePayer":         ["editCasePayer"],
    "addTreatmentNote":      ["addTreatmentNote"],
    # CONFIRMED live 2026-08-03: the endpoint is getPatientFiles.
    "getPatientDocuments":   ["getPatientFiles", "getPatientDocuments",
                              "getFiles"],
    "getDocumentURL":        ["getDocumentURL", "getFileURL", "getFileUrl"],
    "uploadFile":            ["uploadFile"],
    # CONFIRMED live 2026-08-03: activateFile does not exist; the real name
    # is setFileActive. Activation IS required — with it skipped, the files
    # showed up under getPatientFiles with status "2" but never appeared in
    # the Documents tab. status "2" means registered-but-not-active.
    "activateFile":          ["setFileActive", "activateFile"],
    "getExtras":             ["getExtras", "getAllExtraFields",
                              "getExtraFields"],
    "addExtraValue":         ["addExtraValue", "addPatientExtraValue"],
    "getAppointments":       ["getAppointments"],
    "getServiceRedemptions": ["getServiceRedemptions"],
    "addMessageToLog":       ["addMessageToPatientCommunicationLog",
                              "addPatientMessage"],
}

# Methods that modify data → audited, blocked in dry_run.
WRITE_METHODS = {
    "addPatient", "editPatient", "updateMedicareDetails", "updateDVADetails",
    "addCase", "updateCase", "editCasePayer", "addTreatmentNote",
    "uploadFile", "activateFile", "addExtraValue", "addMessageToLog",
}

# Phrases that suggest "this endpoint name doesn't exist" rather than a
# validation failure — used to trigger candidate fallback.
_UNKNOWN_METHOD_MARKERS = (
    "unknown function", "unknown method", "invalid function",
    "invalid method", "no such function", "not found", "does not exist",
    "invalid action", "unknown action", "invalid endpoint",
)

# Nookal serves a themed HTML 404 page for endpoint names it doesn't know,
# rather than a JSON error. Confirmed against the live API 2026-08-03:
# updateMedicareDetails and activateFile both returned this.
_HTML_404_MARKERS = (
    "404 page not found", "page that doesn't exist", "hold it right there",
)


def _looks_like_404_page(text: str) -> bool:
    low = str(text).lower()
    return any(marker in low for marker in _HTML_404_MARKERS)


class NookalClient:
    def __init__(self, config: Optional[NookalConfig] = None,
                 dry_run: bool = False, verbose: bool = False):
        if requests is None:
            raise NookalError("The 'requests' package is required: "
                              "pip install requests")
        self.config = config or NookalConfig.load()
        self.dry_run = dry_run
        self.verbose = verbose
        self._resolved: dict[str, str] = {}       # logical -> confirmed name
        self._session = requests.Session()
        self._cache: dict[str, Any] = {}          # reference-data cache

    # ---------------------------------------------------------------- core

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[nookal] {msg}")

    def _audit(self, method: str, params: dict, outcome: str,
               response: Any = None) -> None:
        entry = {
            "ts": _dt.datetime.now().isoformat(timespec="seconds"),
            "method": method,
            "params": {k: v for k, v in params.items() if k != "api_key"},
            "outcome": outcome,
        }
        if response is not None:
            entry["response"] = response
        try:
            with open(self.config.audit_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:  # never let audit failure break a call
            self._log(f"audit write failed: {exc}")

    def _http(self, endpoint: str, params: dict) -> tuple[int, Any]:
        url = f"{self.config.base_url}/{endpoint}"
        payload = dict(params)
        payload["api_key"] = self.config.api_key
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                if self.config.http_method.upper() == "GET":
                    resp = self._session.get(url, params=payload,
                                             timeout=self.config.timeout)
                else:
                    resp = self._session.post(url, data=payload,
                                              timeout=self.config.timeout)
                if resp.status_code >= 500:
                    raise NookalError(f"server error {resp.status_code}")
                try:
                    return resp.status_code, resp.json()
                except ValueError:
                    return resp.status_code, {"_raw_text": resp.text[:2000]}
            except (requests.ConnectionError, requests.Timeout,
                    NookalError) as exc:
                last_exc = exc
                if attempt < self.config.max_retries:
                    wait = 2 ** attempt
                    self._log(f"{endpoint}: {exc} — retry {attempt} "
                              f"in {wait}s")
                    time.sleep(wait)
        raise NookalError(f"{endpoint}: failed after "
                          f"{self.config.max_retries} attempts: {last_exc}")

    @staticmethod
    def _failure_message(body: Any) -> Optional[str]:
        """Return an error message if the response body indicates failure."""
        if not isinstance(body, dict):
            return "non-JSON response"
        status = str(body.get("status", "")).lower()
        if status in ("failure", "fail", "error"):
            details = body.get("details") or body.get("message") \
                or body.get("data") or body
            if isinstance(details, dict):
                details = details.get("errorMessage") \
                    or details.get("message") or json.dumps(details)[:500]
            return str(details)
        raw = body.get("_raw_text")
        if raw is not None:
            if _looks_like_404_page(raw):
                # Nookal answers an unrecognised endpoint with an HTML 404
                # page, not a JSON error. Saying "not found" here routes it
                # into the candidate-name fallback instead of surfacing a
                # wall of markup as a validation failure.
                return "not found: Nookal returned its HTML 404 page"
            return f"non-JSON response: {raw[:200]}"
        return None

    def call(self, logical: str, **params: Any) -> dict:
        """Call a logical method, resolving candidate endpoint names."""
        if logical not in CANDIDATES:
            raise NookalError(f"unknown logical method {logical!r}")
        clean = {k: v for k, v in params.items() if v is not None}

        if logical in WRITE_METHODS and self.dry_run:
            self._log(f"DRY RUN {logical} {clean}")
            self._audit(logical, clean, "dry_run")
            return {"dry_run": True, "method": logical, "params": clean}

        names: Iterable[str] = (
            [self._resolved[logical]] if logical in self._resolved
            else CANDIDATES[logical]
        )
        last_error: Optional[NookalAPIError] = None
        for name in names:
            status, body = self._http(name, clean)
            failure = self._failure_message(body)
            if failure is None and status < 400:
                self._resolved[logical] = name
                if logical in WRITE_METHODS:
                    self._audit(name, clean, "success", body)
                return body
            msg = failure or f"HTTP {status}"
            looks_unknown = status == 404 or any(
                m in msg.lower() for m in _UNKNOWN_METHOD_MARKERS)
            err_cls = UnknownMethodError if looks_unknown else NookalAPIError
            last_error = err_cls(name, msg, payload=body, http_status=status)
            if not looks_unknown:
                break  # real validation error — don't mask it with fallbacks
            self._log(f"{name} rejected as unknown; trying next candidate")
        if logical in WRITE_METHODS and last_error is not None:
            self._audit(logical, clean, f"error: {last_error}")
        raise last_error  # type: ignore[misc]

    # ------------------------------------------------------- data digging

    @staticmethod
    def extract(body: Any, *keys: str) -> Any:
        """Search a response for keys IN PRIORITY ORDER.

        Nookal wraps results at varying depths ({"data":{"results":{...}}}).
        extract(body, "cases", "results") exhausts the whole structure for
        "cases" before ever considering "results", so a specific key always
        beats a generic fallback regardless of nesting depth.
        """
        for key in keys:
            stack = [body]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    if key in node:
                        return node[key]
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
        return None

    # ------------------------------------------------------ reference data

    def verify(self) -> dict:
        return self.call("verify")

    def locations(self, refresh: bool = False) -> list:
        if refresh or "locations" not in self._cache:
            body = self.call("getLocations")
            self._cache["locations"] = self.extract(
                body, "locations", "Locations") or []
        return self._cache["locations"]

    def practitioners(self, refresh: bool = False) -> list:
        if refresh or "practitioners" not in self._cache:
            body = self.call("getPractitioners")
            self._cache["practitioners"] = self.extract(
                body, "practitioners", "Practitioners") or []
        return self._cache["practitioners"]

    def services(self, refresh: bool = False) -> list:
        if refresh or "services" not in self._cache:
            body = self.call("getServices")
            self._cache["services"] = self.extract(
                body, "services", "Services", "appointmentTypes") or []
        return self._cache["services"]

    # ----------------------------------------------------------- patients

    def search_patients_exact(self, first_name: str, last_name: str,
                              dob: str) -> list:
        """Exact-match search (dob = YYYY-MM-DD). Returns possibly-empty list."""
        body = self.call("searchPatients", first_name=first_name,
                         last_name=last_name, date_of_birth=dob)
        return self.extract(body, "patients", "Patients", "results") or []

    def search_patients_fuzzy(self, last_name: str,
                              first_name: Optional[str] = None) -> list:
        body = self.call("searchPatients", last_name=last_name,
                         first_name=first_name, fuzzy_search="true")
        return self.extract(body, "patients", "Patients", "results") or []

    def add_patient(self, first_name: str, last_name: str, dob: str,
                    **optional: Any) -> dict:
        """optional: email, mobile, home, work, address_line_1..3, city,
        state, postcode, country, client_notes, alert_notes, ...
        NOTE the addPatient doc example shows 'phone' but the field table
        lists home/mobile/work — diagnostic phase 2 settles which is real.
        """
        return self.call("addPatient", first_name=first_name,
                         last_name=last_name, date_of_birth=dob, **optional)

    def edit_patient(self, patient_id: int, **fields: Any) -> dict:
        return self.call("editPatient", patient_id=patient_id, **fields)

    def update_medicare(self, patient_id: int, medicare_no: str,
                        medicare_irn: str,
                        expiry_date: Optional[str] = None) -> dict:
        """Number and IRN must be supplied together (API rule). Validates the
        check digit locally first and refuses to send an invalid number."""
        if not medicare_number_valid(medicare_no):
            raise NookalError(
                f"Medicare number {medicare_no!r} fails check-digit "
                "validation — refusing to write it to Nookal.")
        return self.call("updateMedicareDetails", patient_id=patient_id,
                         medicare_no=medicare_no, medicare_irn=medicare_irn,
                         expiry_date=expiry_date)

    def search_or_create_patient(
        self, first_name: str, last_name: str, dob: str,
        create_fields: Optional[dict] = None,
    ) -> tuple[Optional[dict], str]:
        """Returns (patient_or_None, disposition).

        disposition:
          'matched'        exactly one exact match — safe to use
          'ambiguous'      several exact matches — human must choose
          'fuzzy_review'   no exact match but near-misses exist — human
                           must confirm before anything is written
          'created'        no plausible match; a new patient was created
                           (or would be, in dry_run)
        A new record is only created when both exact and fuzzy search come
        back empty — duplicate patients are worse than a pause for review.
        """
        exact = self.search_patients_exact(first_name, last_name, dob)
        if len(exact) == 1:
            return exact[0], "matched"
        if len(exact) > 1:
            return None, "ambiguous"
        near = self.search_patients_fuzzy(last_name, first_name)
        if near:
            return None, "fuzzy_review"
        body = self.add_patient(first_name, last_name, dob,
                                **(create_fields or {}))
        if body.get("dry_run"):
            return body, "created"
        created = self.extract(body, "patient", "patients", "results")
        if isinstance(created, list):
            created = created[0] if created else None
        return created, "created"

    # -------------------------------------------------------------- cases

    CASE_TITLE = "GP CCMP"   # must match the existing dropdown option EXACTLY

    def get_cases(self, patient_id: int) -> list:
        body = self.call("getCases", patient_id=patient_id)
        return self.extract(body, "cases", "Cases", "results") or []

    def add_case(self, patient_id: int, title: Optional[str] = None,
                 referral_date: Optional[str] = None,
                 notes: Optional[str] = None,
                 primary_provider_id: Optional[int] = None,
                 referrer_id: Optional[int] = None) -> dict:
        """Create a case. Title defaults to CASE_TITLE ('GP CCMP').

        CONFIRMED live 2026-08-03: the Title dropdown is built from the
        titles currently in use, not a curated clinic-wide list, and an
        API-created case titled 'GP CCMP' matched the existing option
        exactly — it appeared once, not twice. A novel title is therefore
        not destructive; it just adds a stray entry that disappears when
        the case is deleted. The lock stays anyway, because a typo'd title
        would silently create a second bucket of CCMP cases that no report
        or filter would pick up.
        Case 'Referrer' is the *marketing-source* field, not the referring
        doctor (that lives on the payer) — hence referrer_id defaults to
        None and should almost always stay None.
        """
        title = title or self.CASE_TITLE
        if title != self.CASE_TITLE and not getattr(
                self, "_any_title_ok", False):
            raise NookalError(
                f"Case title {title!r} != {self.CASE_TITLE!r}. Novel titles "
                "pollute the clinic-wide dropdown. Call allow_any_title() "
                "first if this is intentional (e.g. the diagnostic).")
        return self.call("addCase", patient_id=patient_id, title=title,
                         referral_date=referral_date, notes=notes,
                         primary_provider_id=primary_provider_id,
                         referrer_id=referrer_id)

    def allow_any_title(self) -> None:
        self._any_title_ok = True

    def update_case(self, patient_id: int, case_id: int,
                    **fields: Any) -> dict:
        return self.call("updateCase", patient_id=patient_id,
                         case_id=case_id, **fields)

    def edit_case_payer(self, patient_id: int, payer_id: int,
                        **fields: Any) -> dict:
        """Semantics of payer_id are unconfirmed (payer type vs existing
        link) — see diagnostic phase 5 before using in production."""
        return self.call("editCasePayer", patient_id=patient_id,
                         payer_id=payer_id, **fields)

    # ---------------------------------------------------------- documents

    def upload_pdf(self, patient_id: int, file_bytes: bytes, name: str,
                   case_id: Optional[int] = None,
                   mime: str = "application/pdf",
                   extension: str = "pdf",
                   activate_before_put: bool = False,
                   put_retries: int = 3) -> dict:
        """Three-step upload: register metadata -> HTTP PUT to presigned S3
        URL -> activateFile. Returns {'file_id':..., 'register':...,
        'put_status':..., 'activate':...}.

        The docs' activateFile note reads inverted ('will fail if the file
        has been uploaded') — activate_before_put=True lets the diagnostic
        test the other ordering. Presigned URLs live ~30 min; on PUT failure
        we re-register and retry with a fresh URL.
        """
        result: dict[str, Any] = {}
        for attempt in range(1, put_retries + 1):
            reg = self.call("uploadFile", patient_id=patient_id,
                            case_id=case_id, name=name, extension=extension,
                            file_type=mime, file_path=f"{name}.{extension}")
            result["register"] = reg
            if reg.get("dry_run"):
                return result
            url = self.extract(reg, "url", "URL", "uploadUrl",
                               "presigned_url", "presignedUrl")
            file_id = self.extract(reg, "file_id", "fileId", "fileID")
            result["file_id"] = file_id
            if not url or not file_id:
                raise NookalAPIError(
                    "uploadFile", "no presigned URL / file_id in response "
                    "(see result['register'])", payload=reg)

            def _activate() -> dict:
                return self.call("activateFile", patient_id=patient_id,
                                 file_id=file_id)

            if activate_before_put:
                result["activate"] = _activate()
            put = requests.put(url, data=file_bytes,
                               headers={"Content-Type": mime},
                               timeout=max(self.config.timeout, 120))
            result["put_status"] = put.status_code
            if put.status_code in (200, 201, 204):
                if not activate_before_put:
                    result["activate"] = _activate()
                return result
            self._log(f"S3 PUT failed ({put.status_code}), attempt "
                      f"{attempt}/{put_retries}; re-registering")
            time.sleep(2 * attempt)
        raise NookalError(f"S3 PUT failed after {put_retries} attempts "
                          f"(last status {result.get('put_status')})")

    def get_patient_documents(self, patient_id: int) -> list:
        body = self.call("getPatientDocuments", patient_id=patient_id)
        return self.extract(body, "files", "Files", "documents",
                            "results") or []

    # ------------------------------------------------- session accounting

    def count_completed_appointments(
        self, patient_id: int, date_from: str,
        date_to: Optional[str] = None,
        service_id: Optional[int] = None,
    ) -> int:
        """Sessions used since a referral date, counted client-side because
        the API cannot link appointments to cases. Filters on
        appt_status='Completed'; pass service_id to restrict to your CCMP
        appointment type (verify the id via services())."""
        total, page = 0, 1
        while True:
            body = self.call("getAppointments", patient_id=patient_id,
                             date_from=date_from, date_to=date_to,
                             appt_status="Completed", service_id=service_id,
                             page=page, page_length=200)
            items = self.extract(body, "appointments", "Appointments",
                                 "results") or []
            total += len(items)
            if len(items) < 200:
                return total
            page += 1

    def get_service_redemptions(self, patient_id: int,
                                **filters: Any) -> dict:
        return self.call("getServiceRedemptions", patient_id=patient_id,
                         **filters)

    # ------------------------------------------------------------- extras

    def get_extras(self) -> Any:
        return self.call("getExtras")

    def add_extra_value(self, patient_id: int, extra_id: int,
                        value: str) -> dict:
        return self.call("addExtraValue", patient_id=patient_id,
                         extra_id=extra_id, value=str(value))


# --------------------------------------------------------------------------
# Referral -> case-notes formatter (shared by pipeline and review screen)
# --------------------------------------------------------------------------

def format_case_notes(referral: dict) -> str:
    """Render an extracted-referral dict into the structured notes block
    written to the Nookal case. Fields the automation cannot write (payer
    sessions, IRN, sex) are surfaced here so the manual step is a copy-read,
    not a PDF hunt. Keys are all optional."""
    r = referral
    flag = "  [VERIFY]" if r.get("services_flagged") else ""
    lines = [
        f"CCMP referral received {r.get('received_date', 'unknown')}",
        f"Referral signed: {r.get('referral_date', 'unknown')}",
        f"GP: {r.get('gp_name', 'unknown')} "
        f"({r.get('gp_provider_number', 'no provider no.')})",
    ]
    if r.get("gp_practice"):
        lines.append(f"Practice: {r['gp_practice']}")
    lines.append(
        f"Services: {r.get('services_count', '?')} — "
        f"{r.get('discipline', 'discipline not stated')}"
        f" (item {r.get('item_number', '?')}){flag}")
    if r.get("medicare_no"):
        lines.append(f"Medicare: {r['medicare_no']} (IRN not on referral — "
                     "capture from card)")
    if r.get("conditions"):
        conds = r["conditions"]
        lines.append("Conditions: " + (", ".join(conds)
                     if isinstance(conds, list) else str(conds)))
    if r.get("form_type"):
        lines.append(f"Form: {r['form_type']}")
    lines.append("-- entered by referral automation; payer/sessions/expiry "
                 "set manually in Add Payer --")
    return "\n".join(lines)
