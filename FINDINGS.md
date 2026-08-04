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
  in the UI. It is readable but not writable, which is what makes the
  post-hoc session-cap check possible.
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

## Payers — created by hand, and not editable via the API

Settled 2026-08-04.

**No endpoint creates a payer.** Nine plausible names (`addCasePayer`,
`createCasePayer`, `addPayer`, `createPayer`, `addPatientPayer`,
`addCaseFunder`, `setCasePayer`, `updateCasePayer`, `addCasePayers`) all
returned Nookal's HTML 404 page. Only `editCasePayer` exists. **Adding the
payer stays a manual step in the UI** — that part is now settled on
evidence.

**`editCasePayer` requires `case_id`.** Probed without one it answers
`"One of Patient ID | Case ID is missing"`. The first attempt sent only
`patient_id` and `payer_id`, which is why it looked like a silent no-op —
the call was simply incomplete.

**It still does not write.** Four attempts, every one returning `success`
and changing nothing:

| `case_id` | `payer_id` | Reading of `payer_id` | Result |
|---|---|---|---|
| absent | 999999 | link | rejected: "Patient ID \| Case ID missing" |
| 3466 | 962 | the real link on that case | no change |
| 3466 | 962 | with Nookal's own field names | no change |
| 3469 (empty case) | 3 | payer *type*, to attach one | no payer appeared |

`Reference` never moved either — free text, name read straight off the
record, nothing to fail validation. Both readings of `payer_id` were tried,
against a case that had a payer and a case that had none.

**Treat `editCasePayer` as unusable.** We cannot say what it does; we can
say that nothing we can construct makes it do anything observable. The only
cheap avenue left is asking Nookal support directly what it does and what
parameters it expects — worth an email, not more probing.

**This matters less than it looks.** Editing a payer is only valuable if
creating one is automated, and creating one is impossible. By the time an
operator is in the Add Payer wizard typing the session count, there is
nothing left for an edit call to save.

What *is* worth building is verification — see below.

**A payer record looks like this** (read back through `getCases`):

```json
{
  "ID": "962",              "payer": "Medicare",
  "Sessions_Approved": "5", "Sessions_Completed": "0",
  "ReferralDate": "0000-00-00", "ExpiryDate": null,
  "Reference": "", "Notes": "", "Status": "1",
  "DateOfInjury": null, "budget": "0.00",
  "caseManager": "", "referrer": ""
}
```

Two things follow. The session cap field is **`Sessions_Approved`** — the
first edit attempt sent `sessions`, a name that does not exist. And
**Nookal maintains `Sessions_Completed` itself**, which was not the
question being asked but answers a bigger one: see the next section.

## Session counting — Nookal already does it

`Sessions_Completed` on the payer means the number of used sessions can be
**read**, rather than derived by counting appointments client-side and
hoping the filters work.

That largely retires the open phase 7 and phase 9 questions
(`getServiceRedemptions`, and whether the `appt_status` / `service_id`
filters actually filter). Worth confirming against a real patient with
history before relying on it, but it is a much shorter path than
appointment counting.

## Payers — the risk that made this field different

The session cap lives in the payer, and **Sessions = 0 means Unlimited** in
Nookal, silently. Since the API cannot write the payer, the mitigation is
to read it back: `verify_payer()` compares `Sessions_Approved` against what
the referral said (or the standard 5) and refuses to close a referral until
they agree. That catches a forgotten payer, one added to the wrong case, a
mistyped count, and a 0.

Appointments separately cannot be linked to a case through the API, so
booking stays manual regardless.

## Still open

Both remaining questions are about counting sessions, and both are now
lower priority because `Sessions_Completed` on the payer reports usage
directly. Worth settling only if that turns out to be unreliable:

* **`getServiceRedemptions`** — never run. Needs a real patient with an
  active CCMP.
* **`appt_status` / `service_id` filters on `getAppointments`** — accepted,
  but the test patient has no appointments, and a silently-ignored filter
  also returns an empty list. Inconclusive until run against a patient with
  history.

* **What `editCasePayer` actually does** — only answerable by Nookal
  support.

Both are read-only:

```bash
python3 diagnostic.py --phases 7,9 \
    --patient-id <real patient with CCMP history> \
    --redemptions-patient-id <same id>
```
