"""Transport layer: HTTP verb, retries, failure parsing, name fallback."""

from __future__ import annotations

import unittest
from unittest import mock

from nookal_client import (NookalAPIError, NookalError,  # noqa: E402
                           UnknownMethodError)

from .base import ClientTestCase
from .fake_nookal import NOOKAL_404_PAGE, UNKNOWN_FUNCTION, failure, success


def no_sleep():
    """Collapse the client's backoff so retry tests run instantly."""
    return mock.patch("nookal_client.time.sleep")


class HttpMethodTests(ClientTestCase):
    def test_post_sends_form_encoded_body(self):
        self.fake.route("verify", success({"ok": True}))
        client = self.make_client()
        client.verify()
        call = self.fake.calls_to("verify")[0]
        self.assertEqual(call.method, "POST")
        self.assertEqual(call.params["api_key"], "TEST-KEY")

    def test_get_sends_query_string(self):
        self.fake.route("verify", success({"ok": True}))
        client = self.make_client(http_method="GET")
        client.verify()
        call = self.fake.calls_to("verify")[0]
        self.assertEqual(call.method, "GET")
        self.assertEqual(call.params["api_key"], "TEST-KEY")

    def test_api_key_is_added_by_the_client_not_the_caller(self):
        self.fake.route("getLocations", success({"locations": []}))
        client = self.make_client()
        client.locations()
        self.assertIn("api_key", self.fake.last_params("getLocations"))

    def test_none_valued_params_are_dropped(self):
        self.fake.route("addCase", success({"case": {"ID": 1}}))
        client = self.make_client()
        client.add_case(5, referral_date=None, notes="x",
                        primary_provider_id=None)
        params = self.fake.last_params("addCase")
        self.assertNotIn("referral_date", params)
        self.assertNotIn("primary_provider_id", params)
        self.assertNotIn("referrer_id", params)
        self.assertEqual(params["notes"], "x")


class RetryTests(ClientTestCase):
    def test_server_error_is_retried_then_succeeds(self):
        calls = {"n": 0}

        def flaky(_params):
            calls["n"] += 1
            if calls["n"] < 3:
                return 503, {"status": "failure"}
            return success({"ok": True})

        self.fake.route("verify", flaky)
        with no_sleep():
            client = self.make_client()
            client.verify()
        self.assertEqual(self.fake.count("verify"), 3)

    def test_server_error_exhausts_retries_and_raises(self):
        self.fake.route("verify", (500, {"status": "failure"}))
        with no_sleep():
            client = self.make_client(max_retries=2)
            with self.assertRaises(NookalError) as ctx:
                client.verify()
        self.assertEqual(self.fake.count("verify"), 2)
        self.assertIn("failed after 2 attempts", str(ctx.exception))

    def test_client_error_is_not_retried(self):
        """A 4xx is the API's answer, not a transient fault — retrying it
        would just repeat a rejected write."""
        self.fake.route("verify", (400, failure("bad request")))
        client = self.make_client()
        with self.assertRaises(NookalAPIError):
            client.verify()
        self.assertEqual(self.fake.count("verify"), 1)

    def test_backoff_is_exponential(self):
        self.fake.route("verify", (500, {}))
        with no_sleep() as sleeper:
            client = self.make_client(max_retries=3)
            with self.assertRaises(NookalError):
                client.verify()
        self.assertEqual([c.args[0] for c in sleeper.call_args_list], [2, 4])


class FailureParsingTests(ClientTestCase):
    def test_failure_status_raises_with_error_message(self):
        self.fake.route("verify", failure("Invalid API key"))
        client = self.make_client()
        with self.assertRaises(NookalAPIError) as ctx:
            client.verify()
        self.assertIn("Invalid API key", str(ctx.exception))

    def test_non_json_body_is_reported_not_swallowed(self):
        self.fake.route("verify", (200, "<html>maintenance</html>"))
        client = self.make_client()
        with self.assertRaises(NookalAPIError) as ctx:
            client.verify()
        self.assertIn("non-JSON response", str(ctx.exception))

    def test_failure_payload_is_attached_to_the_error(self):
        body = failure("Missing parameter: patient_id")
        self.fake.route("verify", body)
        client = self.make_client()
        with self.assertRaises(NookalAPIError) as ctx:
            client.verify()
        self.assertEqual(ctx.exception.payload, body)
        self.assertEqual(ctx.exception.http_status, 200)


class CandidateFallbackTests(ClientTestCase):
    """The printed docs truncate endpoint URLs, so each logical method has
    candidate names tried in order. The risk to guard is a real validation
    error being mistaken for a wrong name and masked by a fallback."""

    def test_falls_through_to_the_working_name(self):
        self.fake.route("getAppointmentTypes", UNKNOWN_FUNCTION)
        self.fake.route("getServices", success({"services": [{"ID": 1}]}))
        client = self.make_client()
        self.assertEqual(client.services(), [{"ID": 1}])
        self.assertEqual(self.fake.sequence(),
                         [("POST", "getAppointmentTypes"),
                          ("POST", "getServices")])

    def test_resolved_name_is_cached_for_the_session(self):
        self.fake.route("getAppointmentTypes", UNKNOWN_FUNCTION)
        self.fake.route("getServices", success({"services": []}))
        client = self.make_client()
        client.services(refresh=True)
        self.fake.reset()
        client.services(refresh=True)
        # Second call must not re-probe the dead name.
        self.assertEqual(self.fake.sequence(), [("POST", "getServices")])
        self.assertEqual(client._resolved["getServices"], "getServices")

    def test_http_404_counts_as_an_unknown_name(self):
        self.fake.route("getAppointmentTypes", (404, {}))
        self.fake.route("getServices", success({"services": []}))
        client = self.make_client()
        client.services()
        self.assertIn("getServices", self.fake.endpoints())

    def test_html_404_page_counts_as_an_unknown_name(self):
        """Nookal answers an unknown endpoint with an HTML 404 page, not a
        JSON error. Confirmed live 2026-08-03 on updateMedicareDetails and
        activateFile — without this the fallback never fires and the caller
        gets a wall of markup instead of a usable error."""
        self.fake.route("getAppointmentTypes", (200, NOOKAL_404_PAGE))
        self.fake.route("getServices", success({"services": [{"ID": 9}]}))
        client = self.make_client()
        self.assertEqual(client.services(), [{"ID": 9}])
        self.assertEqual(self.fake.sequence(),
                         [("POST", "getAppointmentTypes"),
                          ("POST", "getServices")])

    def test_html_404_on_every_candidate_raises_a_readable_error(self):
        self.fake.default_route = (200, NOOKAL_404_PAGE)
        client = self.make_client()
        with self.assertRaises(UnknownMethodError) as ctx:
            client.services()
        message = str(ctx.exception)
        self.assertIn("404", message)
        self.assertNotIn("<html>", message)   # no markup dumped at the user

    def test_validation_error_is_never_masked_by_a_fallback(self):
        """searchPatients rejecting a bad parameter must surface as-is —
        not be retried under the searchPatient alias and reported as a
        naming problem."""
        self.fake.route("searchPatients",
                        failure("Missing required parameter: last_name"))
        self.fake.route("searchPatient", success({"patients": [{"ID": 1}]}))
        client = self.make_client()
        with self.assertRaises(NookalAPIError) as ctx:
            client.search_patients_exact("A", "B", "1990-01-01")
        self.assertNotIsInstance(ctx.exception, UnknownMethodError)
        self.assertIn("last_name", str(ctx.exception))
        self.assertEqual(self.fake.endpoints(), ["searchPatients"])

    def test_all_candidates_unknown_raises_unknown_method_error(self):
        client = self.make_client()   # nothing routed: every name unknown
        with self.assertRaises(UnknownMethodError):
            client.get_patient_documents(1)
        self.assertEqual(self.fake.endpoints(),
                         ["getPatientFiles", "getPatientDocuments", "getFiles"])

    def test_unknown_logical_method_is_a_programming_error(self):
        client = self.make_client()
        with self.assertRaises(NookalError):
            client.call("getUnicorns")
        self.assertEqual(self.fake.endpoints(), [])


class ReferenceDataTests(ClientTestCase):
    def test_reference_data_is_cached_until_refresh(self):
        self.fake.route("getLocations", success({"locations": [{"ID": 3}]}))
        client = self.make_client()
        client.locations()
        client.locations()
        self.assertEqual(self.fake.count("getLocations"), 1)
        client.locations(refresh=True)
        self.assertEqual(self.fake.count("getLocations"), 2)

    def test_missing_collection_yields_empty_list_not_none(self):
        self.fake.route("getPractitioners", success({"unexpected": 1}))
        client = self.make_client()
        self.assertEqual(client.practitioners(), [])


if __name__ == "__main__":
    unittest.main()
