"""The three-step document upload: register -> S3 PUT -> activate.

The printed docs' activateFile note reads inverted, so both orderings are
supported and both are pinned here.
"""

from __future__ import annotations

import unittest
from unittest import mock

from nookal_client import NookalAPIError, NookalError

from .base import ClientTestCase
from .fake_nookal import success

PDF = b"%PDF-1.1\n%%EOF\n"


class UploadTests(ClientTestCase):
    def setUp(self):
        super().setUp()
        self.fake.route("uploadFile", lambda p: success(
            {"url": self.fake.s3_url(), "file_id": 77}))
        self.fake.route("activateFile", success({"ok": True}))

    def no_sleep(self):
        return mock.patch("nookal_client.time.sleep")

    # ------------------------------------------------------- happy paths

    def test_register_then_put_then_activate(self):
        client = self.make_client()
        result = client.upload_pdf(1, PDF, "referral")
        self.assertEqual(result["put_status"], 200)
        self.assertEqual(result["file_id"], 77)
        self.assertIn("activate", result)
        self.assertEqual(self.fake.sequence(),
                         [("POST", "uploadFile"),
                          ("PUT", "/s3/upload"),
                          ("POST", "activateFile")])

    def test_file_bytes_reach_the_presigned_url_intact(self):
        client = self.make_client()
        client.upload_pdf(1, PDF, "referral")
        put = self.fake.calls_to("/s3/upload")[0]
        self.assertEqual(put.body, PDF)

    def test_activate_before_put_ordering(self):
        client = self.make_client()
        client.upload_pdf(1, PDF, "referral", activate_before_put=True)
        self.assertEqual(self.fake.sequence(),
                         [("POST", "uploadFile"),
                          ("POST", "activateFile"),
                          ("PUT", "/s3/upload")])

    def test_activate_is_called_once_only(self):
        client = self.make_client()
        client.upload_pdf(1, PDF, "referral")
        self.assertEqual(self.fake.count("activateFile"), 1)

    def test_case_id_is_forwarded_when_supplied(self):
        client = self.make_client()
        client.upload_pdf(1, PDF, "referral", case_id=55)
        params = self.fake.last_params("uploadFile")
        self.assertEqual(params["case_id"], "55")
        self.assertEqual(params["patient_id"], "1")
        self.assertEqual(params["name"], "referral")
        self.assertEqual(params["extension"], "pdf")
        self.assertEqual(params["file_type"], "application/pdf")

    def test_case_id_omitted_when_absent(self):
        client = self.make_client()
        client.upload_pdf(1, PDF, "referral")
        self.assertNotIn("case_id", self.fake.last_params("uploadFile"))

    def test_204_from_s3_counts_as_success(self):
        self.fake.put_status = 204
        client = self.make_client()
        result = client.upload_pdf(1, PDF, "referral")
        self.assertEqual(result["put_status"], 204)
        self.assertEqual(self.fake.count("activateFile"), 1)

    # ---------------------------------------------------------- failures

    def test_expired_url_is_retried_with_a_fresh_registration(self):
        """Presigned URLs live ~30 min; on PUT failure the client must
        re-register for a new URL rather than replay the dead one."""
        self.fake.put_statuses = [403, 403]      # then falls back to 200
        with self.no_sleep():
            client = self.make_client()
            result = client.upload_pdf(1, PDF, "referral")
        self.assertEqual(result["put_status"], 200)
        self.assertEqual(self.fake.count("uploadFile"), 3)
        self.assertEqual(self.fake.count("/s3/upload"), 3)
        self.assertEqual(self.fake.count("activateFile"), 1)

    def test_persistent_put_failure_raises_after_the_retry_budget(self):
        self.fake.put_status = 403
        with self.no_sleep():
            client = self.make_client()
            with self.assertRaises(NookalError) as ctx:
                client.upload_pdf(1, PDF, "referral", put_retries=2)
        self.assertIn("403", str(ctx.exception))
        self.assertEqual(self.fake.count("uploadFile"), 2)
        self.assertEqual(self.fake.count("activateFile"), 0)

    def test_missing_presigned_url_is_a_clear_error(self):
        self.fake.route("uploadFile", success({"file_id": 77}))
        client = self.make_client()
        with self.assertRaises(NookalAPIError) as ctx:
            client.upload_pdf(1, PDF, "referral")
        self.assertIn("presigned URL", str(ctx.exception))
        self.assertEqual(self.fake.count("activateFile"), 0)

    def test_missing_file_id_is_a_clear_error(self):
        self.fake.route("uploadFile",
                        lambda p: success({"url": self.fake.s3_url()}))
        client = self.make_client()
        with self.assertRaises(NookalAPIError):
            client.upload_pdf(1, PDF, "referral")
        self.assertEqual(self.fake.count("/s3/upload"), 0)

    def test_dry_run_registers_nothing_and_uploads_nothing(self):
        client = self.make_client(dry_run=True)
        result = client.upload_pdf(1, PDF, "referral")
        self.assertTrue(result["register"]["dry_run"])
        self.assertEqual(self.fake.sequence(), [])


class DocumentListTests(ClientTestCase):
    def test_documents_unwrapped_from_any_of_the_known_keys(self):
        for key in ("files", "Files", "documents"):
            with self.subTest(key=key):
                self.fake.route("getPatientDocuments",
                                success({key: [{"ID": 1}]}))
                client = self.make_client()
                self.assertEqual(client.get_patient_documents(1), [{"ID": 1}])


if __name__ == "__main__":
    unittest.main()
