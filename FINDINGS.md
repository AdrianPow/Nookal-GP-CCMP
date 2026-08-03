# Confirmed Nookal API behaviour

Everything here was verified against the live Embrace Movement Clinic
account (ID 11726) on **2026-08-03**, not inferred from the printed docs.
Where the docs and the API disagreed, the API won.

## Endpoint names

The printed reference truncates URLs, and four names in it are wrong. The
client's candidate-fallback found two; `probe_endpoints.py` found the other
two by calling ten alternates each with `patient_id=0` and watching for a
validation error instead of Nookal's HTML 404 page.

| In the docs | Actually |
|---|---|
| `getServices` | **`getAppointmentTypes`** |
| `getPatientDocuments` | **`getPatientFiles`** |
| `updateMedicareDetails` | **`updatePatientMedicareDetails`** |
| `activateFile` | **`setFileActive`** (handler echoes `setFileAsActive`) |

Unknown endpoint names return an **HTML 404 page**, not a JSON error — the
client now recognises that page so the fallback fires instead of dumping
markup at the caller.

`getAppointments` is accepted but the handler reports itself as
`getPatientAppointments` when a `patient_id` is supplied.

## Transport

* **POST**, form-encoded. Confirmed by phase 0.
* Responses wrap results at varying depth: `{"status":…,"data":{"results":…}}`.
* Presigned S3 upload URLs live **30 minutes** (`X-Amz-Expires=1800`) and
  point at **ap-southeast-2** — Nookal already stores documents in AWS
  Sydney.

## Reference data

* One location, ID `1`.
* Practitioners: `1` Nicole Saxby, `5` Laura Bradford (both Physiotherapy).
* **`ServiceCode` on each service carries the Medicare item number** —
  "Medicare EP (BB)" is service ID `3`, ServiceCode `10953`. Item numbers
  read off a referral map straight to the right appointment type; nothing
  needs hardcoding.

## Cases

* **`addCase` with title `GP CCMP` matches the existing option exactly** —
  it appears once in the dropdown, not twice. Automated case creation is
  safe. This was the highest-priority open question.
* The Title dropdown is **built from titles currently in use**, not a
  curated clinic-wide list. A novel title is not destructive; it vanishes
  when the case is deleted. The client still locks the title to `GP CCMP`,
  because a typo would silently create a second bucket of CCMP cases that
  no report or filter would surface.
* Creating a case auto-creates a **Documents folder of the same name**.
* Case records expose a **`payers` array** — empty until a payer is added
  in the UI, but present, so the session cap can likely be *read back* and
  checked against the referral.
* Case `referrerType` holds values like "Other" — it is the marketing
  source, **not** the referring doctor, and carries no contact ID. Building
  a GP lookup table from case history is not possible; the GP has to come
  from each referral.

## Medicare

* Written via `updatePatientMedicareDetails`.
* Number and IRN are stored as one `Reference`: `"2123456701-1"`.
* **Expiry keeps only year and month.** `2027-11-30` was stored as
  `"2027-11"` and the Health tab shows `11 / 2027`. A Medicare card only
  prints MM/YY, so extraction never needs to invent a day — any day passes.
* Records land with `Verified: 0` — "Unverified" in the UI, which is
  correct when nobody has sighted the card.

## Documents

Three steps, in this order: `uploadFile` (register) → HTTP PUT to the
presigned URL → `setFileActive`.

* **`status` is the field that matters.** `"2"` means registered but
  invisible in the Documents tab; `"1"` means active and listed. A skipped
  activation leaves a file that `getPatientFiles` returns happily and staff
  never see — the failure that looks like success.
* Passing `case_id` to `uploadFile` puts the file **inside that case's
  folder**. Confirmed end to end: activated + attached → PDF appears in the
  `GP CCMP` folder.

## Payers — staying manual, permanently

`editCasePayer` accepted a `payer_id` of `999999` and returned **success**,
while `payers` stayed empty. It does not validate its input and did not do
what it claimed. Combined with the session cap living in the payer wizard —
where **Sessions = 0 means Unlimited** — payer creation stays in the UI.
The client will not write payers.

Appointments also cannot be linked to a case through the API, so booking
stays manual too.

## Still open

* **`getServiceRedemptions`** — never run. Needs a real patient with an
  active CCMP. Decides whether Medicare allocations can be read directly
  instead of counting appointments.
* **`appt_status` / `service_id` filters on `getAppointments`** — accepted,
  but the test patient has no appointments, and a silently-ignored filter
  also returns an empty list. Inconclusive until run against a patient with
  history.

Both are read-only:

```bash
python3 diagnostic.py --phases 7,9 \
    --patient-id <real patient with CCMP history> \
    --redemptions-patient-id <same id>
```
