"""Claude Code provider.

`--permission-mode auto` with `--add-dir CWD` is the non-prompting headless
posture; `-p ... --output-format stream-json` gives parseable events. A bare
`claude -p` defaults to prompts with no host to answer them, so the mode flag
is mandatory. No trust gate
exists for `-p`. `--read-only` uses `--permission-mode plan` (claude's own
no-edit mode) and also denies the edit tools, plus WebSearch/WebFetch unless
`--web`. It is an app-level guard; core's post-run delta check backs it.
"""

from __future__ import annotations

import json

from .base import Event, Job, Provider


class Claude(Provider):
    name = "claude"
    runner = "claude"
    parses_stream = True
    read_only = "app"
    web_toggle = True
    answers = True

    def argv(self, job: Job) -> list[str]:
        args = ["-p", "--permission-mode", "plan" if job.read_only else "auto"]
        if job.read_only:
            denied = ["Edit", "Write", "NotebookEdit"]
            if not job.web:
                denied += ["WebSearch", "WebFetch"]
            args += ["--disallowed-tools", ",".join(denied)]
        args += ["--add-dir", str(job.cwd),
                "--output-format", "stream-json", "--verbose",
                "--no-session-persistence"]
        if job.max_turns:
            args += ["--max-turns", str(job.max_turns)]
        if job.model:
            args += ["--model", job.model]
        if job.effort:
            args += ["--effort", job.effort]
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
        t = d.get("type")
        if t == "result":
            stop = d.get("subtype") or "end_turn"
            turns = _int(d.get("num_turns"))
            cost = _float(d.get("total_cost_usd") or d.get("cost_usd"))
            text = d.get("result") if isinstance(d.get("result"), str) else None
            if d.get("is_error"):
                why = text or "error"
                status = d.get("api_error_status")
                if status is not None and status != "":
                    why = f"{why} (HTTP {status})"
                return Event(kind="error", text=why, stop_reason=stop,
                             turns=turns, cost_usd=cost)
            return Event(kind="end", stop_reason=stop, turns=turns,
                         cost_usd=cost, text=text)
        if t == "assistant":
            return _from_message(d.get("message"))
        if t == "user":
            # tool_result blocks arrive as a user message echo.
            return _from_tool_result(d.get("message"))
        return None


def _from_message(msg) -> Event | None:
    if not isinstance(msg, dict):
        return None
    for block in msg.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            inp = block.get("input") or {}
            path = None
            if isinstance(inp, dict):
                path = Provider._first(inp, "file_path", "path", "filename", "notebook_path")
            return Event(kind="tool_call", name=block.get("name"), path=path)
        if block.get("type") == "text":
            return Event(kind="text", text=block.get("text"))
    return None


def _from_tool_result(msg) -> Event | None:
    if not isinstance(msg, dict):
        return None
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            ok = not bool(block.get("is_error"))
            return Event(kind="tool_result", ok=ok)
    return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
