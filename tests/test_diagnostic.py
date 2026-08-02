"""End-to-end tests for diagnostic.py, driven against the fake API.

The point of these is the rehearsal guarantee: `--dry-run` must complete a
full pass without issuing a single write, because the first real run happens
against live clinic data.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

import diagnostic
from nookal_client import CANDIDATES, WRITE_METHODS, NookalClient

from .base import ClientTestCase
from .fake_nookal import failure, success

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "diagnostic.py")

# Endpoint names any write would appear under, including the alternates the
# candidate-fallback registry might try.
WRITE_ENDPOINT_NAMES = {name for logical in WRITE_METHODS
                        for name in CANDIDATES[logical]}


class GetIdTests(unittest.TestCase):
    """get_id has to survive every shape Nookal wraps an id in."""

    def get_id(self, body, *keys):
        return diagnostic.get_id(NookalClient, body, *keys)

    def test_dict_with_uppercase_id(self):
        self.assertEqual(self.get_id({"patient": {"ID": 42}}, "patient"), 42)

    def test_dict_with_lowercase_id(self):
        self.assertEqual(self.get_id({"patient": {"id": 42}}, "patient"), 42)

    def test_dict_with_entity_specific_key(self):
        self.assertEqual(self.get_id({"case": {"case_id": 7}}, "case"), 7)

    def test_list_takes_the_first_element(self):
        self.assertEqual(
            self.get_id({"patients": [{"ID": 1}, {"ID": 2}]}, "patients"), 1)

    def test_bare_value(self):
        self.assertEqual(self.get_id({"patient": 42}, "patient"), 42)

    def test_numeric_string_is_coerced(self):
        self.assertEqual(self.get_id({"patient": "42"}, "patient"), 42)

    def test_missing_key_is_none(self):
        self.assertIsNone(self.get_id({"other": 1}, "patient"))

    def test_empty_list_is_none(self):
        self.assertIsNone(self.get_id({"patients": []}, "patients"))

    def test_unrecognisable_shape_is_none(self):
        self.assertIsNone(self.get_id({"patient": {"name": "Jane"}},
                                      "patient"))

    def test_dry_run_body_yields_no_id(self):
        self.assertIsNone(self.get_id({"dry_run": True}, "patient"))


class IsDryTests(unittest.TestCase):
    def test_recognises_the_marker(self):
        self.assertTrue(diagnostic.is_dry({"dry_run": True}))

    def test_real_response_is_not_dry(self):
        self.assertFalse(diagnostic.is_dry({"status": "success"}))

    def test_non_dict_is_not_dry(self):
        for value in (None, [], "x", 0):
            self.assertFalse(diagnostic.is_dry(value))


class DiagnosticRunTests(ClientTestCase):
    """Runs the real script as a subprocess against the fake server."""

    def setUp(self):
        super().setUp()
        self.config_path = os.path.join(self.tmp.name, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"api_key": "TEST-KEY",
                       "base_url": self.fake.base_url,
                       "audit_log": self.audit_path,
                       "http_method": "POST"}, f)
        self.fake.routes_from({
            "verify": success({"ok": True}),
            "getLocations": success({"locations": [{"ID": 1,
                                                    "name": "Clinic"}]}),
            "getPractitioners": success({"practitioners": [{"ID": 2}]}),
            "getServices": success({"services": [{"ID": 3,
                                                  "name": "Physio"}]}),
            "getCases": success({"cases": []}),
            "getAllCases": success({"cases": []}),
            "getAppointments": success({"appointments": []}),
            "getPatientDocuments": success({"files": []}),
            "getServiceRedemptions": success({"redemptions": []}),
        })

    def run_diagnostic(self, *args, timeout=120):
        return subprocess.run(
            [sys.executable, SCRIPT, "--config", self.config_path, *args],
            cwd=self.tmp.name, capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
        )

    def report_text(self) -> str:
        path = os.path.join(self.tmp.name, "diagnostic_report.txt")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def writes_attempted(self) -> list[str]:
        return [e for e in self.fake.endpoints()
                if e in WRITE_ENDPOINT_NAMES]

    # ------------------------------------------------------------ dry run

    def test_dry_run_completes_without_writing_anything(self):
        result = self.run_diagnostic("--yes", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.writes_attempted(), [])

    def test_dry_run_still_confirms_auth_and_reference_data(self):
        self.run_diagnostic("--yes", "--dry-run")
        self.assertGreaterEqual(self.fake.count("verify"), 1)
        self.assertEqual(self.fake.count("getLocations"), 1)
        self.assertEqual(self.fake.count("getServices"), 1)

    def test_dry_run_is_announced_in_the_report(self):
        self.run_diagnostic("--yes", "--dry-run")
        self.assertIn("DRY RUN", self.report_text())

    def test_dry_run_claims_nothing_that_did_not_happen(self):
        self.run_diagnostic("--yes", "--dry-run")
        report = self.report_text()
        for false_claim in ("case created, case_id",
                            "A nonsense payer_id SUCCEEDED",
                            "PUT status"):
            self.assertNotIn(false_claim, report)

    def test_dry_run_records_intended_writes_in_the_audit_log(self):
        self.run_diagnostic("--yes", "--dry-run")
        outcomes = {e["outcome"] for e in self.audit_entries()}
        self.assertEqual(outcomes, {"dry_run"})
        self.assertGreater(len(self.audit_entries()), 0)

    # --------------------------------------------------------- read phases

    def test_read_only_phases_issue_no_writes(self):
        result = self.run_diagnostic("--yes", "--phases", "0,1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.writes_attempted(), [])
        self.assertIn("PHASE 1", self.report_text())

    def test_phase0_reports_the_working_http_method(self):
        self.run_diagnostic("--yes", "--phases", "0")
        self.assertIn("authenticates using HTTP POST", self.report_text())

    def test_phase0_falls_back_to_get_when_post_fails(self):
        """The docs don't say which verb Nookal wants; phase 0 tries the
        configured one, then the other, and reports the winner."""
        def get_only(_params):
            if self.fake.calls[-1].method == "GET":
                return success({"ok": True})
            return 400, failure("POST not supported")

        self.fake.route("verify", get_only)
        result = self.run_diagnostic("--yes", "--phases", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("authenticates using HTTP GET", self.report_text())

    def test_total_auth_failure_stops_before_the_write_phases(self):
        self.fake.route("verify", failure("Invalid API key"))
        result = self.run_diagnostic("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Authentication failed", self.report_text())
        self.assertEqual(self.writes_attempted(), [])

    # -------------------------------------------------------------- errors

    def test_missing_config_aborts_before_any_call(self):
        result = subprocess.run(
            [sys.executable, SCRIPT, "--config",
             os.path.join(self.tmp.name, "absent.json"), "--yes"],
            cwd=self.tmp.name, capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "NOOKAL_API_KEY": ""},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("CONFIG ERROR", self.report_text())
        self.assertEqual(self.fake.sequence(), [])

    def test_transient_server_error_in_reference_data_does_not_abort(self):
        self.fake.route("getLocations", (503, {"status": "failure"}))
        result = self.run_diagnostic("--yes", "--dry-run", "--phases", "0,1,2,3")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report_text()
        self.assertIn("locations FAILED", report)
        self.assertIn("PHASE 3", report)   # the run carried on past it

    def test_transient_server_error_in_a_later_phase_does_not_abort(self):
        """A 5xx surfaces as a bare NookalError, not NookalAPIError. The
        later phases used to catch only the latter, so one flaky call would
        kill the run partway and leave the test patient half-configured —
        with the remaining phases unanswered."""
        self.fake.route("getCases", (503, {"status": "failure"}))
        result = self.run_diagnostic("--yes", "--dry-run", "--phases",
                                     "0,2,6,9")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report_text()
        self.assertIn("getCases failed", report)
        self.assertIn("PHASE 9", report)   # the run reached the last phase


if __name__ == "__main__":
    unittest.main()
