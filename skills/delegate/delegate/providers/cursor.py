"""Cursor Agent provider (passthrough parser).

`--print --force --trust` is the non-prompting headless posture. Cursor has no
generic effort flag — the model slug owns that — so --effort is accepted for
interface parity and not forwarded. `--read-only` adds `--mode plan` (cursor's
own no-edit mode). It is an app-level guard; core's post-run delta check backs it.
Only the stream's final `result` line is parsed, for the answer.
"""

from __future__ import annotations

import json

from .base import Event, Job, Provider


class Cursor(Provider):
    name = "cursor"
    runner = "cursor-agent"
    parses_stream = True
    read_only = "app"
    answers = True

    def argv(self, job: Job) -> list[str]:
        args = ["--print", "--workspace", str(job.cwd), "--trust", "--force",
                "--output-format", "stream-json"]
        if not job.no_sandbox:
            args += ["--sandbox", "enabled"]
        else:
            args += ["--sandbox", "disabled"]
        if job.read_only:
            args += ["--mode", "plan"]
        if job.model:
            args += ["--model", job.model]
        # --effort deliberately not forwarded (model slug owns effort).
        args += ["--", job.prompt]
        return args

    def parse(self, line: str) -> Event | None:
        line = line.strip()
        if not line or line[0] != "{":
            return None
        try:
            d = json.loads(line)
        except ValueError:
            return None
        if d.get("type") == "result" and isinstance(d.get("result"), str):
            return Event(kind="end", stop_reason=d.get("subtype") or "end_turn",
                         text=d["result"])
        return None
