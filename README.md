# Nookal CCMP referral automation — client + diagnostic

Two files, built from the API reference you captured (Patients, Clinic,
Appointments sections) and the UI screens (Case, Payer, Health, Profile).

| File | Purpose |
|---|---|
| `nookal_client.py` | Reusable API client: patient search-or-create, Medicare details, case creation (`GP CCMP`), 3-step PDF upload, session counting, validators, audit log, dry-run. |
| `diagnostic.py` | One-shot probe that answers every question the docs left open. Run this FIRST, before any real automation. |
| `config.example.json` | Copy to `nookal_config.json`, paste your API key. |
| `tests/` | Offline suite — runs the whole client against a fake Nookal on localhost. No API key, no network, nothing touched in the clinic. |

## Setup (5 minutes)

```bash
pip install -r requirements.txt
cp config.example.json nookal_config.json
# edit nookal_config.json — paste the API key from Nookal Practice Setup
python3 -m unittest discover -t . -s tests   # offline, ~50s, no key needed
python3 diagnostic.py --dry-run --yes        # rehearsal against the real API
python3 diagnostic.py                        # the real run
```

`nookal_config.json` holds your live API key and is gitignored — keep it
that way.

The script prompts before every write. All writes go to a throwaway patient
it creates (`ZZTEST APIDIAG`, DOB 1990-01-01) with a checksum-valid fake
Medicare number. Nothing touches real patient records.

Useful variants:

```bash
python3 diagnostic.py --dry-run --yes            # rehearsal: every write is
                                                 # a logged no-op; reads still
                                                 # confirm auth, endpoint names
                                                 # and reference data
python3 diagnostic.py --yes                      # no prompts
python3 diagnostic.py --phases 0,1               # read-only phases only
python3 diagnostic.py --patient-id 123           # reuse existing ZZTEST
python3 diagnostic.py --redemptions-patient-id N # phase 7: real patient
                                                 # with an active CCMP
                                                 # (read-only)
```

Everything is written to `diagnostic_report.txt` — send me that file and
I build the extraction layer against confirmed facts instead of inferred
ones.

## What each phase settles

0. Auth works; POST vs GET (config auto-hint printed).
1. Location / practitioner / service IDs; the service-record field names.
2. Whether `addPatient` takes `phone` or only `home/mobile/work`.
3. **Highest priority** — does `addCase` titled `GP CCMP` match your
   existing dropdown option, and what does a novel title do (add junk /
   free text / rejected). This decides whether case creation is safe to
   automate. You confirm in the UI; the API can't see the dropdown.
4. Medicare expiry: API wants `Y-m-d`, UI shows month/year — what displays.
5. `editCasePayer`: what `payer_id` actually references (error-message
   probe, then optional real-payer retest after you add one in the UI).
6. Whether `getCases`/`getAllCases` expose referrer/payer contact IDs
   (decides if a GP lookup table can be harvested from history).
7. What `getServiceRedemptions` returns — Medicare allocations or class
   packs.
8. Upload order: register → S3 PUT → activate, or activate-first (the docs
   note reads inverted; both orders are tried if the first fails).
9. `appt_status` / `service_id` filters on `getAppointments`, and whether
   `service_id` == `appointment_type_id`.

## Testing without touching Nookal

```bash
python3 -m unittest discover -t . -s tests        # everything
python3 -m unittest tests.test_validators -v      # one module
```

126 tests, stdlib only — no pytest, no API key, no network. `tests/
fake_nookal.py` is a real HTTP server on localhost that speaks Nookal's
response envelope, so the suite exercises the actual request path (form
encoding, GET vs POST, retries, the presigned-URL PUT) rather than a
mocked-out stub.

What it pins down, beyond the validators:

* the endpoint-name fallback resolves and caches the working name — and a
  genuine validation error is **never** masked by trying the next candidate
* `search_or_create_patient` creates a record only when exact *and* fuzzy
  search both come back empty
* `add_case` refuses any title but `GP CCMP` before a request goes out
* `update_medicare` refuses an invalid check digit before a request goes out
* the three-step upload in both orderings, plus re-registration when a
  presigned URL has expired
* `dry_run` issues no writes at all, and the audit log never contains the
  API key
* `diagnostic.py --dry-run` completes a full pass with zero writes, and
  reports nothing it did not actually observe

The suite is checked by mutation: deliberately breaking each of those rails
in turn is caught by at least one test (8/8 at last run).

## Clean-up after the diagnostic

* Delete patient **ZZTEST APIDIAG** in the Nookal UI.
* If phase 3's novel-title test added **ZZ DIAGNOSTIC TITLE TEST** to the
  case Title dropdown, remove it.
* `nookal_audit.jsonl` holds a record of every write the tools made.

## Design decisions baked into the client

* **Case title is locked to `GP CCMP`.** The Title field is a managed
  clinic-wide dropdown; novel strings risk polluting it, so the client
  refuses them unless explicitly overridden.
* **No payer creation, no appointment booking.** The payer wizard carries
  the session cap, and Sessions=0 means *Unlimited* — a silent failure if
  automated. Appointments can't be linked to a case via the API. Both stay
  manual in the UI, with everything you need pre-formatted into the case
  notes (`format_case_notes`).
* **Medicare numbers are check-digit validated before any write**, and the
  update refuses invalid numbers outright. Provider numbers have a
  validator too (both algorithms verified against your three sample
  referrals).
* **Duplicate-patient protection:** a new record is created only when both
  exact and fuzzy search return nothing. One exact match → use it; several
  → `ambiguous`; fuzzy near-misses → `fuzzy_review`. Both of the latter
  stop and wait for a human.
* **Endpoint-name fallback:** the printed docs truncate URLs, so each
  logical method has candidate names tried in order; the working name is
  cached and reported. Genuine validation errors are never masked by
  fallback.
* Every write is appended to a JSONL **audit log** (API key never logged),
  and `dry_run=True` turns all writes into log-only no-ops.

## What comes after the diagnostic

1. You run it, check the four UI items it flags, send back
   `diagnostic_report.txt`.
2. I adjust the client to the confirmed endpoint names/shapes and build the
   extraction layer (OCR → field extraction → validation) against your
   three sample formats.
3. Then the review screen and mailbox ingestion, in that order.
