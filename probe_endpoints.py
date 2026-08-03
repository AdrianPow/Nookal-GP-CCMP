#!/usr/bin/env python3
"""
probe_endpoints.py — find the real names of the endpoints the docs got wrong.

The 2026-08-03 diagnostic showed two endpoints answering with Nookal's HTML
404 page, meaning those names simply don't exist:

    updateMedicareDetails   (phase 4 — writing Medicare number/IRN/expiry)
    activateFile            (phase 8 — the third step of a document upload)

This script tries plausible alternates and tells you which ones are real.

Why it's safe to run
--------------------
Every call is made with patient_id=0, which matches no patient in your
account. A name that exists answers with a validation error ("no such
patient", "missing parameter") — which is all we need to know it's real. A
name that doesn't exist answers with the 404 page. Nothing is created,
updated or deleted, and no real patient is named in any request.

A control group of known-good endpoints runs too, so you can see the probe
telling the difference rather than taking its word for it.

Usage:
    python3 probe_endpoints.py
    python3 probe_endpoints.py --config nookal_config.json

Results are written to endpoint_probe.txt — send that back.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from nookal_client import (NookalClient, NookalConfig, NookalError,
                           _looks_like_404_page)

REPORT_PATH = "endpoint_probe.txt"

# An id that matches nothing, so an endpoint that exists can only answer
# with a validation error.
SAFE_PARAMS = {"patient_id": 0}

PROBES: list[tuple[str, list[str]]] = [
    ("Medicare details — replaces the missing updateMedicareDetails", [
        "updateMedicareDetails",
        "editPatientMedicare",
        "updatePatientMedicare",
        "editMedicareDetails",
        "editMedicare",
        "updateMedicare",
        "addMedicareDetails",
        "setMedicareDetails",
        "updatePatientMedicareDetails",
        "editPatientMedicareDetails",
    ]),
    ("File activation — replaces the missing activateFile", [
        "activateFile",
        "activatePatientFile",
        "activateUpload",
        "activateFileUpload",
        "completeUpload",
        "confirmUpload",
        "finaliseFile",
        "finalizeFile",
        "setFileActive",
        "uploadFileComplete",
    ]),
    ("DVA details — same family, likely the same naming mistake", [
        "updateDVADetails",
        "editPatientDVA",
        "updatePatientDVA",
        "editDVADetails",
    ]),
    ("CONTROL — these are known to exist; all should report EXISTS", [
        "verify",
        "getPatients",
        "getAppointmentTypes",
        "getPatientFiles",
    ]),
    ("CONTROL — this is invented; it should report MISSING", [
        "definitelyNotARealNookalEndpoint",
    ]),
]

MISSING = "MISSING "
EXISTS = "EXISTS  "
ACCEPTED = "EXISTS! "
UNCLEAR = "UNCLEAR "


def classify(status: int, body) -> tuple[str, str]:
    """Return (verdict, short explanation) for one probed name."""
    if isinstance(body, dict) and "_raw_text" in body:
        if _looks_like_404_page(body["_raw_text"]):
            return MISSING, "Nookal's HTML 404 page — this name does not exist"
        return UNCLEAR, f"non-JSON response: {body['_raw_text'][:70]}"
    if status == 404:
        return MISSING, "HTTP 404"
    message = NookalClient._failure_message(body)
    if message is None:
        return ACCEPTED, ("real, and returned SUCCESS for patient_id=0 — "
                          "normal for a read that ignores the id, worth a "
                          "closer look if it writes")
    return EXISTS, f"rejected our params: {message[:110]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--config", default="nookal_config.json")
    args = ap.parse_args()

    try:
        client = NookalClient(NookalConfig.load(args.config))
    except NookalError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1

    lines: list[str] = [
        f"Nookal endpoint probe — {dt.datetime.now():%Y-%m-%d %H:%M}",
        "=" * 70,
        "Every call uses patient_id=0, which matches no patient. Nothing is",
        "written. EXISTS means the name is real and rejected our deliberately",
        "invalid parameters, which is the answer we want.",
        "",
    ]

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    found: dict[str, list[str]] = {}
    for group, names in PROBES:
        say(f"\n{group}")
        say("-" * len(group))
        for name in names:
            try:
                status, body = client._http(name, dict(SAFE_PARAMS))
            except NookalError as exc:
                say(f"  {UNCLEAR} {name:<32} network/server error: {exc}")
                continue
            verdict, detail = classify(status, body)
            say(f"  {verdict} {name:<32} {detail}")
            if verdict in (EXISTS, ACCEPTED):
                found.setdefault(group, []).append(name)

    say("\n" + "=" * 70)
    say("SUMMARY — names that exist:")
    if found:
        for group, names in found.items():
            say(f"  {group}")
            for name in names:
                say(f"      {name}")
    else:
        say("  none — every probed name returned the 404 page")
    say("")
    say("If the two CONTROL groups came out as expected (known names EXISTS,")
    say("the invented one MISSING), the verdicts above can be trusted.")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    say(f"\nWritten to {REPORT_PATH} — send that back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
