"""Shared fixture: a client wired to a FakeNookal and a throwaway audit log."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nookal_client import NookalClient, NookalConfig  # noqa: E402

from .fake_nookal import FakeNookal  # noqa: E402


class ClientTestCase(unittest.TestCase):
    """Each test gets a fresh server, a fresh client and an empty audit log."""

    def setUp(self) -> None:
        self.fake = FakeNookal()
        self.addCleanup(self.fake.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit_path = os.path.join(self.tmp.name, "audit.jsonl")

    def make_client(self, *, dry_run: bool = False, **overrides) -> NookalClient:
        cfg = NookalConfig(
            api_key="TEST-KEY",
            base_url=self.fake.base_url,
            audit_log=self.audit_path,
            **overrides,
        )
        return NookalClient(cfg, dry_run=dry_run)

    # ------------------------------------------------------------- audit

    def audit_entries(self) -> list[dict]:
        if not os.path.exists(self.audit_path):
            return []
        with open(self.audit_path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def audit_text(self) -> str:
        if not os.path.exists(self.audit_path):
            return ""
        with open(self.audit_path, encoding="utf-8") as f:
            return f.read()
