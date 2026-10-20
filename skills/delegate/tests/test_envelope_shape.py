"""Schema-3 envelope shape.

The whole point of the envelope is a stable, exhaustive key set: every exit path
writes the SAME object, so an orchestrator can index any field without probing.
These tests pin the exact key set and the schemaVersion.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delegate.envelope import (  # noqa: E402
    SCHEMA_VERSION,
    STOP_CANCELLED,
    STOP_END,
    STOP_ERROR,
    STOP_TIMEOUT,
    Envelope,
)

# The complete, exact top-level key set of a schema-3 envelope.
EXPECTED_KEYS = {
    "schemaVersion", "provider", "runId", "cwd",
    "stopReason", "exitCode", "error",
    "turns", "costUsd", "failedToolCalls", "model",
    "baseCommit", "dirtyBefore", "diffPath", "diffStat", "emptyDiff",
    "scope", "scopeViolations", "readOnly", "answerPath",
    "reportPath", "reportExists",
    "logPath", "streamPath",
    "durationSeconds", "dryRun", "providerExtra",
}


class TestEnvelopeShape(unittest.TestCase):
    def _rich(self) -> Envelope:
        return Envelope(
            provider="grok",
            run_id="20260101T000000-abcd",
            cwd="/repo",
            stop_reason=STOP_END,
            exit_code=0,
            error=None,
            turns=4,
            cost_usd=0.1234,
            failed_tool_calls=1,
            model="grok-4",
            base_commit="deadbeef",
            dirty_before=True,
            diff_path="/state/run/diff.patch",
            diff_stat=" 2 files changed, 10 insertions(+)",
            empty_diff=False,
            scope=["src/", "tests/"],
            scope_violations=["etc/passwd"],
            report_path="/repo/REPORT.md",
            report_exists=True,
            log_path="/state/run/runner.log",
            stream_path="/state/run/stream.jsonl",
            duration_seconds=42,
            dry_run=False,
            provider_extra={"argv": ["grok", "--single", "hi"], "raw": 1},
        )

    def test_exact_key_set(self) -> None:
        obj = self._rich().to_json()
        self.assertEqual(set(obj.keys()), EXPECTED_KEYS)

    def test_minimal_envelope_has_all_keys(self) -> None:
        # Even the barest construction emits every key (orchestrator contract).
        obj = Envelope(provider="claude", run_id="r1", cwd="/x").to_json()
        self.assertEqual(set(obj.keys()), EXPECTED_KEYS)

    def test_schema_version_is_three(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 3)
        self.assertEqual(self._rich().to_json()["schemaVersion"], 3)

    def test_value_types_and_mapping(self) -> None:
        obj = self._rich().to_json()
        self.assertEqual(obj["provider"], "grok")
        self.assertEqual(obj["runId"], "20260101T000000-abcd")
        self.assertEqual(obj["cwd"], "/repo")
        self.assertEqual(obj["stopReason"], "end_turn")
        self.assertIsInstance(obj["exitCode"], int)
        self.assertIsInstance(obj["failedToolCalls"], int)
        self.assertIsInstance(obj["dirtyBefore"], bool)
        self.assertIsInstance(obj["emptyDiff"], bool)
        self.assertIsInstance(obj["dryRun"], bool)
        self.assertIsInstance(obj["scope"], list)
        self.assertIsInstance(obj["scopeViolations"], list)
        self.assertIsInstance(obj["providerExtra"], dict)
        self.assertIsInstance(obj["durationSeconds"], int)

    def test_falsy_strings_normalise_to_none(self) -> None:
        # to_json coerces empty strings on optional fields to null.
        obj = Envelope(provider="claude", run_id="r1", cwd="/x").to_json()
        self.assertIsNone(obj["stopReason"])
        self.assertIsNone(obj["error"])
        self.assertIsNone(obj["model"])
        self.assertIsNone(obj["baseCommit"])
        self.assertIsNone(obj["diffPath"])
        self.assertIsNone(obj["reportPath"])
        # reportExists defaults to None (not yet checked), not False.
        self.assertIsNone(obj["reportExists"])

    def test_stop_reason_constants(self) -> None:
        self.assertEqual(STOP_END, "end_turn")
        self.assertEqual(STOP_CANCELLED, "cancelled")
        self.assertEqual(STOP_TIMEOUT, "timeout")
        self.assertEqual(STOP_ERROR, "error")


if __name__ == "__main__":
    unittest.main()
