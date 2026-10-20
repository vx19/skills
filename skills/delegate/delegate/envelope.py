"""Schema-3 result envelope.

One compact JSON object is written to stdout on every exit path. Schema 3 is a
provider-neutral core plus a `providerExtra` bag for fields only one provider
can supply (a raw provider status string, usage tokens, etc.).

All CLI providers use core.run and this envelope.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 3

# Terminal states. A run ends in exactly one.
STOP_END = "end_turn"          # provider finished normally
STOP_CANCELLED = "cancelled"   # killed via `delegate cancel`, or provider self-cancel
STOP_TIMEOUT = "timeout"       # exceeded the outer wall clock
STOP_ERROR = "error"           # provider crashed / non-success exit / preflight failure


def dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def write(obj: dict[str, Any]) -> None:
    sys.stdout.write(dumps(obj))
    sys.stdout.write("\n")
    sys.stdout.flush()


@dataclass
class Envelope:
    """Every field is assigned once; `to_json` serialises the whole object.

    Deliberately no inline `diff`: a large patch in the envelope blew up
    orchestrator context. The delta always lives on `diffPath`; `diffStat`
    carries the scannable summary.
    """

    provider: str
    run_id: str
    cwd: str
    # outcome
    stop_reason: str | None = None
    exit_code: int = 0
    error: str | None = None
    # worker signal
    turns: int | None = None
    cost_usd: float | None = None
    failed_tool_calls: int = 0
    model: str | None = None
    # repository delta
    base_commit: str | None = None
    dirty_before: bool = False
    diff_path: str | None = None
    diff_stat: str | None = None
    empty_diff: bool = True
    scope: list[str] = field(default_factory=list)
    scope_violations: list[str] = field(default_factory=list)
    read_only: bool = False
    # the worker's final message, written by core (or the runner) after the run
    answer_path: str | None = None
    # report contract
    report_path: str | None = None
    report_exists: bool | None = None
    # artefacts
    log_path: str | None = None
    stream_path: str | None = None
    # bookkeeping
    duration_seconds: int = 0
    dry_run: bool = False
    provider_extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "provider": self.provider,
            "runId": self.run_id,
            "cwd": self.cwd,
            "stopReason": self.stop_reason or None,
            "exitCode": int(self.exit_code),
            "error": self.error or None,
            "turns": self.turns,
            "costUsd": self.cost_usd,
            "failedToolCalls": int(self.failed_tool_calls or 0),
            "model": self.model or None,
            "baseCommit": self.base_commit or None,
            "dirtyBefore": bool(self.dirty_before),
            "diffPath": self.diff_path or None,
            "diffStat": self.diff_stat or None,
            "emptyDiff": bool(self.empty_diff),
            "scope": list(self.scope),
            "scopeViolations": list(self.scope_violations),
            "readOnly": bool(self.read_only),
            "answerPath": self.answer_path or None,
            "reportPath": self.report_path or None,
            "reportExists": self.report_exists,
            "logPath": self.log_path or None,
            "streamPath": self.stream_path or None,
            "durationSeconds": int(self.duration_seconds or 0),
            "dryRun": bool(self.dry_run),
            "providerExtra": dict(self.provider_extra),
        }

    def emit(self) -> None:
        write(self.to_json())
