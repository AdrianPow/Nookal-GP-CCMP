"""Audit log and dry-run.

Two promises the project rests on: every write is recorded, and the API key
is never written to disk.
"""

from __future__ import annotations

import os
import unittest

from nookal_client import WRITE_METHODS, NookalAPIError

from .base import ClientTestCase
from .fake_nookal import failure, success


class DryRunTests(ClientTestCase):
    def test_write_makes_no_request_and_returns_a_marker(self):
        client = self.make_client(dry_run=True)
        result = client.add_patient("Jane", "Doe", "1990-01-01")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["method"], "addPatient")
        self.assertEqual(result["params"]["first_name"], "Jane")
        self.assertEqual(self.fake.sequence(), [])

    def test_reads_still_reach_the_api_in_dry_run(self):
        """dry_run suppresses writes only — the diagnostic still needs real
        reference data to report on."""
        self.fake.route("getLocations", success({"locations": [{"ID": 1}]}))
        client = self.make_client(dry_run=True)
        self.assertEqual(client.locations(), [{"ID": 1}])
        self.assertEqual(self.fake.count("getLocations"), 1)

    def test_every_write_method_is_suppressed(self):
        client = self.make_client(dry_run=True)
        for method in sorted(WRITE_METHODS):
            with self.subTest(method=method):
                result = client.call(method, patient_id=1)
                self.assertTrue(result.get("dry_run"))
        self.assertEqual(self.fake.sequence(), [])

    def test_dry_run_writes_are_still_audited(self):
        client = self.make_client(dry_run=True)
        client.add_case(5, notes="rehearsal")
        entries = self.audit_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["outcome"], "dry_run")
        self.assertEqual(entries[0]["method"], "addCase")


class AuditTests(ClientTestCase):
    def test_successful_write_is_recorded_with_the_resolved_name(self):
        self.fake.route("addPatient", success({"patient": {"ID": 42}}))
        client = self.make_client()
        client.add_patient("Jane", "Doe", "1990-01-01")
        entries = self.audit_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["method"], "addPatient")
        self.assertEqual(entries[0]["outcome"], "success")
        self.assertEqual(entries[0]["params"]["last_name"], "Doe")
        self.assertIn("response", entries[0])
        self.assertIn("ts", entries[0])

    def test_failed_write_is_recorded_as_an_error(self):
        self.fake.route("addPatient", failure("Duplicate patient"))
        client = self.make_client()
        with self.assertRaises(NookalAPIError):
            client.add_patient("Jane", "Doe", "1990-01-01")
        entries = self.audit_entries()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["outcome"].startswith("error:"))
        self.assertIn("Duplicate patient", entries[0]["outcome"])

    def test_api_key_is_never_written_to_the_audit_log(self):
        self.fake.route("addPatient", success({"patient": {"ID": 42}}))
        self.fake.route("updateMedicareDetails", success({"ok": True}))
        client = self.make_client()
        client.add_patient("Jane", "Doe", "1990-01-01")
        client.update_medicare(42, "4081334278", "1")
        self.assertNotIn("TEST-KEY", self.audit_text())
        self.assertNotIn("api_key", self.audit_text())

    def test_reads_are_not_audited(self):
        self.fake.route("getLocations", success({"locations": []}))
        client = self.make_client()
        client.locations()
        self.assertEqual(self.audit_entries(), [])

    def test_one_line_of_json_per_write(self):
        self.fake.route("addPatient", success({"patient": {"ID": 42}}))
        client = self.make_client()
        for i in range(3):
            client.add_patient(f"P{i}", "Doe", "1990-01-01")
        with open(self.audit_path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3)

    def test_unwritable_audit_log_does_not_break_the_call(self):
        """Losing the audit trail is bad; losing the referral because the
        log path was wrong is worse."""
        self.fake.route("addPatient", success({"patient": {"ID": 42}}))
        client = self.make_client()
        client.config.audit_log = os.path.join(
            self.tmp.name, "no-such-dir", "audit.jsonl")
        result = client.add_patient("Jane", "Doe", "1990-01-01")
        self.assertEqual(client.extract(result, "patient"), {"ID": 42})


if __name__ == "__main__":
    unittest.main()
