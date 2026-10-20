"""Pi provider.

The `--mode json` stream is parsed for turns, cost, tool calls, and the final
answer. Pi has no built-in sandbox — it runs with the caller's full privileges — and
never prompts for trust non-interactively; the CLI prints one stderr warning
about the missing sandbox. Provider/model come from --provider-arg.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .base import Event, Job, Provider, usage_tokens


class Pi(Provider):
    name = "pi"
    runner = "pi"
    parses_stream = True
    answers = True
    # Signals the CLI to print the unsandboxed warning line once.
    unsandboxed = True
    extra_keys = frozenset({"provider"})

    def argv(self, job: Job) -> list[str]:
        args = ["-p", job.prompt, "--no-session", "--no-extensions", "--mode", "json"]
        provider = job.extra.get("provider")
        if provider:
            args += ["--provider", provider]
        if job.model:
            args += ["--model", job.model]
        if job.effort:
            args += ["--thinking", job.effort]
        return args

    def preflight_check(self, job: Job) -> str | None:
        raw = os.environ.get("PI_CODING_AGENT_DIR")
        agent = Path(raw).expanduser() if raw else Path.home() / ".pi" / "agent"

        def denied(exc: OSError) -> str | None:
            detail = exc.strerror or exc.__class__.__name__
            return (
                f"pi cannot write its config dir {agent} ({detail}); "
                "the calling sandbox must allow writes there"
            )

        # Pi creates its settings and auth locks with mkdir, so probe the same way.
        probe = agent / f"delegate-write-probe-{os.getpid()}-{time.monotonic_ns()}"
        try:
            os.mkdir(probe)
            os.rmdir(probe)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            try:
                os.rmdir(probe)
            except OSError:
                pass
            return denied(exc)
        return None

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
        return _parse_line(data)


_WRITE_NAMES = frozenset({"write", "edit"})
_USAGE_FIELDS = (
    ("input", "input"),
    ("output", "output"),
    ("reasoning", "reasoning"),
    ("cacheRead", "cache_read"),
)


def _parse_line(data: dict) -> Event | None:
    kind = data.get("type")
    if kind == "message_update":
        return _message_update(data)
    if kind == "tool_execution_end":
        return _tool_result(data)
    if kind == "message_end":
        return _assistant_usage(data)
    # turn_end repeats the assistant message's usage; count the turn only.
    # "turn", not "end": a mid-run status line must not say "last end".
    if kind == "turn_end":
        return Event(kind="turn", turns_delta=1)
    return None


def _message_update(data: dict) -> Event | None:
    event = data.get("assistantMessageEvent")
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if kind == "thinking_delta":
        return Event(kind="thought")
    if kind == "text_delta":
        delta = event.get("delta")
        return Event(kind="text", text=delta if isinstance(delta, str) else None)
    if kind == "toolcall_end":
        return _tool_call(event)
    return None


def _tool_call(event: dict) -> Event:
    call = event.get("toolCall")
    if not isinstance(call, dict):
        call = {}
    name = call.get("name")
    if not isinstance(name, str):
        name = None
    path = _argument_path(call.get("arguments"))
    return Event(kind="tool_call", name=name, path=path,
                 write=name in _WRITE_NAMES)


def _argument_path(arguments) -> str | None:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    if not isinstance(arguments, dict):
        return None
    found = Provider._first(arguments, "path", "file_path")
    return found if isinstance(found, str) else None


def _tool_result(data: dict) -> Event:
    name = data.get("toolName")
    if not isinstance(name, str):
        name = None
    return Event(kind="tool_result", name=name, ok=not data.get("isError"))


def _assistant_usage(data: dict) -> Event | None:
    message = data.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    usage = message.get("usage")
    tokens = None
    cost = None
    if isinstance(usage, dict):
        tokens = usage_tokens(usage, _USAGE_FIELDS)
        cost_obj = usage.get("cost")
        if isinstance(cost_obj, dict):
            total = cost_obj.get("total")
            if not isinstance(total, bool) and isinstance(total, (int, float)):
                cost = float(total)
    return Event(kind="usage", tokens=tokens, cost_usd=cost)

