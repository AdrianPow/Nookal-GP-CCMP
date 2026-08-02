"""Config loading — the step every first run trips over."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from nookal_client import NookalConfig, NookalError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, data: dict) -> str:
        path = os.path.join(self.tmp.name, "nookal_config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_loads_from_file(self):
        path = self.write({"api_key": "abc", "http_method": "GET",
                           "audit_log": "a.jsonl"})
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = NookalConfig.load(path)
        self.assertEqual(cfg.api_key, "abc")
        self.assertEqual(cfg.http_method, "GET")
        self.assertEqual(cfg.audit_log, "a.jsonl")

    def test_defaults_fill_in_the_rest(self):
        path = self.write({"api_key": "abc"})
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = NookalConfig.load(path)
        self.assertEqual(cfg.base_url, "https://api.nookal.com/production/v2")
        self.assertEqual(cfg.http_method, "POST")
        self.assertEqual(cfg.max_retries, 3)

    def test_environment_variable_overrides_the_file(self):
        path = self.write({"api_key": "from-file"})
        with mock.patch.dict(os.environ, {"NOOKAL_API_KEY": "from-env"}):
            cfg = NookalConfig.load(path)
        self.assertEqual(cfg.api_key, "from-env")

    def test_environment_variable_alone_is_enough(self):
        missing = os.path.join(self.tmp.name, "absent.json")
        with mock.patch.dict(os.environ, {"NOOKAL_API_KEY": "from-env"}):
            cfg = NookalConfig.load(missing)
        self.assertEqual(cfg.api_key, "from-env")

    def test_missing_key_raises_an_actionable_message(self):
        path = self.write({"base_url": "https://example.invalid"})
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(NookalError) as ctx:
                NookalConfig.load(path)
        message = str(ctx.exception)
        self.assertIn("config.example.json", message)
        self.assertIn("NOOKAL_API_KEY", message)

    def test_placeholder_key_from_the_example_file_is_still_a_key(self):
        """The example ships a PASTE-YOUR-... placeholder. It loads, so the
        failure surfaces at the API as an auth error rather than here —
        worth knowing when reading a phase 0 report."""
        path = self.write({"api_key": "PASTE-YOUR-NOOKAL-API-KEY-HERE"})
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = NookalConfig.load(path)
        self.assertEqual(cfg.api_key, "PASTE-YOUR-NOOKAL-API-KEY-HERE")

    def test_unknown_keys_in_the_file_are_ignored(self):
        path = self.write({"api_key": "abc", "future_option": True,
                           "typo_field": "x"})
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = NookalConfig.load(path)
        self.assertEqual(cfg.api_key, "abc")
        self.assertFalse(hasattr(cfg, "future_option"))

    def test_example_config_shipped_in_the_repo_is_loadable(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config.example.json"),
                  encoding="utf-8") as f:
            data = json.load(f)
        path = self.write(data)
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = NookalConfig.load(path)
        self.assertEqual(cfg.base_url, "https://api.nookal.com/production/v2")
        self.assertEqual(cfg.http_method, "POST")


if __name__ == "__main__":
    unittest.main()
