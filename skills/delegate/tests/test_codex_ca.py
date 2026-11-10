"""Codex's TLS roots when it runs from a Grok tool shell.

Under a Grok Build seatbelt on macOS, codex cannot load the keychain's trust
settings: the TCP connect succeeds and the TLS handshake fails. Delegate then
points the codex child at the system PEM. Other harnesses, other platforms and
a bundle the operator already chose stay untouched.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from delegate.providers import codex
from delegate.providers.base import Job


class CodexCaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pem = Path(tmp.name) / "cert.pem"
        self.pem.write_text("pem\n", encoding="utf-8")
        self.job = Job("codex", Path(tmp.name), "x", "t", Path(tmp.name))

    def environment(self, env, platform="darwin", pem=None):
        with patch.dict(os.environ, {"GROK_AGENT": "", "SSL_CERT_FILE": "",
                                     "CODEX_CA_CERTIFICATE": "", **env}), \
             patch.object(sys, "platform", platform), \
             patch.object(codex, "SYSTEM_CA_FILE", str(pem or self.pem)):
            return codex.Codex().environment(self.job)

    def test_a_grok_tool_shell_gets_the_system_pem(self):
        self.assertEqual(self.environment({"GROK_AGENT": "1"}),
                         {"SSL_CERT_FILE": str(self.pem)})

    def test_other_harnesses_keep_the_keychain_roots(self):
        self.assertEqual(self.environment({}), {})

    def test_other_platforms_are_unchanged(self):
        self.assertEqual(self.environment({"GROK_AGENT": "1"}, platform="linux"), {})

    def test_a_bundle_the_operator_chose_wins(self):
        for name in ("SSL_CERT_FILE", "CODEX_CA_CERTIFICATE"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.environment({"GROK_AGENT": "1", name: "/custom/ca.pem"}), {})

    def test_a_missing_system_pem_sets_nothing(self):
        self.assertEqual(self.environment({"GROK_AGENT": "1"},
                                          pem=self.pem.with_name("absent.pem")), {})


if __name__ == "__main__":
    unittest.main()
