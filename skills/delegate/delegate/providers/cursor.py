"""Cursor Agent provider (passthrough parser).

`--print --force --trust` is the non-prompting headless posture. Cursor has no
generic effort flag — the model slug owns that — so --effort is accepted for
interface parity and not forwarded. `--read-only` adds `--mode plan` (cursor's
own no-edit mode). It is an app-level guard; core's post-run delta check backs it.
Only the stream's final `result` line is parsed: the answer, or an error when it
sets `is_error`.
"""

from __future__ import annotations

from .base import Event, Job, Provider


class Cursor(Provider):
    name = "cursor"
    runner = "cursor-agent"
    parses_stream = True
    read_only = "app"
    answers = True
    supports = frozenset({"no_sandbox"})

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

    def events(self, d: dict) -> list[Event]:
        if d.get("type") == "result" and isinstance(d.get("result"), str):
            stop = d.get("subtype") or "end_turn"
            if d.get("is_error"):
                return [Event(kind="error", stop_reason=stop, text=d["result"])]
            return [Event(kind="end", stop_reason=stop, text=d["result"])]
        return []
