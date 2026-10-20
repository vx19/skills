"""Codex provider.

`exec --approve-for-me` is the non-prompting headless posture. `--json`
emits JSONL that is parsed for tool calls, writes, turns, and usage; a stream
`error`/`turn.failed` message becomes the envelope's failure reason. The final
message comes from codex's own `--output-last-message` file. `--read-only`
uses `--sandbox read-only` (kernel) with `approval_policy="never"`, so an
escalation fails instead of prompting.

`--computer-use` relies on Codex's own Computer Use plugin (the `cua_repl` MCP
server Codex Desktop installs). Delegate never grants app access: a headless run
can use an app only where the user already approved it with "Always allow" in an
interactive session. A refused app fails the run with the fix.
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

from .base import Event, Job, Provider, usage_tokens

# What the Computer Use tool returns when the user has not approved an app.
DENIED = re.compile(r"Computer Use was not approved to use ([^\n]+?)\s*$", re.MULTILINE)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def computer_use_status(home: Path | None = None) -> tuple[bool, str]:
    """(ready, detail) for Codex's Computer Use plugin, read offline."""
    home = home or codex_home()
    pattern = home / "plugins/cache/openai-bundled/unified-computer-use/*/.mcp.json"
    for mcp in sorted(glob.glob(str(pattern)), reverse=True):
        try:
            server = json.loads(Path(mcp).read_text(encoding="utf-8"))["mcpServers"]["cua_repl"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        command = server.get("command") or ""
        if not server.get("enabled", True):
            return False, f"cua_repl is disabled in {mcp}"
        if not os.access(command, os.X_OK):
            return False, f"cua_repl runtime missing ({command}); reinstall Codex Desktop"
        return True, f"cua_repl plugin {Path(mcp).parent.name}"
    return False, ("Codex Computer Use plugin not found; enable Computer Use in "
                   "Codex Desktop once")


def denied_apps(stream: Path) -> list[str]:
    """Apps the Computer Use tool refused in this run's stream, in order seen.

    Only MCP tool results count: the worker quoting the refusal in its own
    messages must not be read as a second refusal or mangle the app name."""
    seen: list[str] = []
    try:
        lines = stream.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return seen
    for line in lines:
        try:
            item = json.loads(line).get("item") or {}
        except (ValueError, AttributeError):
            continue
        if item.get("type") != "mcp_tool_call" or not isinstance(item.get("result"), dict):
            continue
        for part in item["result"].get("content") or []:
            text = part.get("text") if isinstance(part, dict) else None
            match = DENIED.match(text or "")
            if match and match.group(1).strip() not in seen:
                seen.append(match.group(1).strip())
    return seen


class Codex(Provider):
    name = "codex"
    runner = "codex"
    parses_stream = True
    read_only = "kernel"
    answers = True
    computer_use = True

    def answer_file(self, job: Job) -> Path | None:
        return job.run_dir / "answer.txt"

    def computer_use_status(self) -> tuple[bool, str]:
        return computer_use_status()

    def argv(self, job: Job) -> list[str]:
        # `--approve-for-me` auto-approves inside codex's workspace-write
        # sandbox and is mutually exclusive with `--sandbox`, so we never pass
        # both. `--no-sandbox` maps to the documented full-access bypass.
        # codex exec takes the prompt as a positional (no `--` separator).
        args = ["exec", "--cd", str(job.cwd), "--skip-git-repo-check",
                "--json", "--ephemeral"]
        if job.no_sandbox:
            args += ["--dangerously-bypass-approvals-and-sandbox"]
        elif job.read_only:
            args += ["--sandbox", "read-only", "-c", 'approval_policy="never"']
        else:
            args += ["--approve-for-me"]
        return args + self._tail(job)

    def _tail(self, job: Job) -> list[str]:
        args = ["--output-last-message", str(self.answer_file(job))]
        if job.model:
            args += ["-m", job.model]
        if job.effort:
            args += ["-c", f'model_reasoning_effort="{job.effort}"']
        return args + [job.prompt]

    def parse(self, line: str) -> Event | None:
        line = line.strip()
        if not line or line[0] != "{":
            return None
        try:
            data = json.loads(line)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return _parse_event(data)

    def finish(self, job: Job, env, returncode: int) -> None:
        if not job.computer_use or not env.stream_path:
            return
        apps = denied_apps(Path(env.stream_path))
        env.provider_extra["computerUseDenied"] = apps
        if apps and env.stop_reason == "end_turn" and env.error is None:
            env.stop_reason, env.exit_code = "error", 1
            env.error = (f"Computer Use was not approved to use {', '.join(apps)}. "
                         "Approve it once in an interactive Codex session ('Always "
                         "allow') and retry.")


_CODEX_USAGE_FIELDS = (
    ("input_tokens", "input"),
    ("cached_input_tokens", "cache_read"),
    ("output_tokens", "output"),
    ("reasoning_output_tokens", "reasoning"),
)


def _parse_event(data: dict) -> Event | None:
    kind = data.get("type")
    if kind in ("item.started", "item.completed"):
        return _item_event(kind, data.get("item"))
    if kind == "turn.completed":
        tokens = usage_tokens(data.get("usage"), _CODEX_USAGE_FIELDS)
        return Event(kind="usage", turns_delta=1, tokens=tokens)
    if kind == "error":
        return _error_event(data.get("message"))
    if kind == "turn.failed":
        # A failed turn still counts: failure streams never reach turn.completed.
        err = data.get("error")
        message = err.get("message") if isinstance(err, dict) else None
        event = _error_event(message)
        if event is not None:
            event.turns_delta = 1
        return event
    return None


def _item_event(event_type: str, item) -> Event | None:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if event_type == "item.started":
        if item_type == "command_execution":
            return Event(kind="tool_call", name="shell")
        return None
    if item_type == "command_execution":
        code = item.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool) and code != 0:
            return Event(kind="tool_result", name="shell", ok=False)
        return None
    if item_type == "file_change":
        return Event(kind="tool_call", name="write",
                     path=_first_changed_path(item.get("changes")), write=True)
    if item_type == "agent_message":
        text = item.get("text")
        return Event(kind="text", text=text if isinstance(text, str) else None)
    return None


def _first_changed_path(changes) -> str | None:
    if not isinstance(changes, list):
        return None
    for change in changes:
        if isinstance(change, dict) and isinstance(change.get("path"), str):
            return change["path"]
    return None


def _error_event(message) -> Event | None:
    if not isinstance(message, str):
        return None
    # An API refusal arrives as a JSON document inside the message string.
    try:
        inner = json.loads(message)["error"]["message"]
    except (ValueError, TypeError, KeyError):
        inner = None
    return Event(kind="error", text=inner if isinstance(inner, str) else message)
