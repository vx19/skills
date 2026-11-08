"""Claude Code provider.

`--permission-mode auto` with `--add-dir CWD` is the non-prompting headless
posture; `-p ... --output-format stream-json` gives parseable events, and
`-p` with no prompt argument reads the prompt from stdin. A bare
`claude -p` defaults to prompts with no host to answer them, so the mode flag
is mandatory. No trust gate
exists for `-p`. `--read-only` uses `--permission-mode plan` (claude's own
no-edit mode) and also denies the edit tools, plus WebSearch/WebFetch unless
`--web`. It is an app-level guard; core's post-run delta check backs it.
"""

from __future__ import annotations

from .base import Event, Job, Provider, as_float, as_int, status_kind

# Claude's file-editing tools: denied under --read-only, counted as writes.
_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# The blocks read from each message role. A message holds several (text, then
# parallel tool calls); the user role only echoes tool results.
_BLOCKS = {"assistant": ("text", "tool_use"), "user": ("tool_result",)}


class Claude(Provider):
    name = "claude"
    runner = "claude"
    parses_stream = True
    read_only = "app"
    web_toggle = True
    answers = True
    supports = frozenset({"effort", "max_turns"})
    prompt_via = "stdin"

    def argv(self, job: Job) -> list[str]:
        args = ["-p", "--permission-mode", "plan" if job.read_only else "auto"]
        if job.read_only:
            denied = list(_EDIT_TOOLS)
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
        return args

    def events(self, d: dict) -> list[Event]:
        t = d.get("type")
        if t == "result":
            stop = d.get("subtype") or "end_turn"
            turns = as_int(d.get("num_turns"))
            cost = as_float(d.get("total_cost_usd") or d.get("cost_usd"))
            text = d.get("result") if isinstance(d.get("result"), str) else None
            if d.get("is_error") or stop.startswith("error"):
                why = text or stop
                status = d.get("api_error_status")
                if status is not None and status != "":
                    why = f"{why} (HTTP {status})"
                kind = "max_turns" if stop == "error_max_turns" else status_kind(status)
                return [Event(kind="error", text=why, stop_reason=stop,
                              turns=turns, cost_usd=cost, error_kind=kind)]
            return [Event(kind="end", stop_reason=stop, turns=turns,
                          cost_usd=cost, text=text)]
        if t == "rate_limit_event":
            info = d.get("rate_limit_info")
            if not isinstance(info, dict) or info.get("status") != "rejected":
                return []
            ts = info.get("resetsAt")
            ok = isinstance(ts, int) and not isinstance(ts, bool)
            return [Event(kind="limit", resets_at=ts if ok else None)]
        msg = d.get("message")
        blocks = msg.get("content") if isinstance(msg, dict) else None
        if t not in _BLOCKS or not isinstance(blocks, list):
            return []
        return [_from_block(b) for b in blocks
                if isinstance(b, dict) and b.get("type") in _BLOCKS[t]]


def _from_block(block: dict) -> Event:
    kind = block["type"]
    if kind == "tool_use":
        inp = block.get("input") or {}
        path = None
        if isinstance(inp, dict):
            path = Provider._first(inp, "file_path", "path", "filename", "notebook_path")
        name = block.get("name")
        return Event(kind="tool_call", name=name, path=path, write=name in _EDIT_TOOLS)
    if kind == "text":
        return Event(kind="text", text=block.get("text"))
    return Event(kind="tool_result", ok=not bool(block.get("is_error")))
