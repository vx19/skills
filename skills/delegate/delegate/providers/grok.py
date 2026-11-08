"""Grok provider.

Launch posture: `--permission-mode auto` (acceptEdits still prompts headlessly
and the missing answer reads as cancel), `--no-plan --no-subagents`,
`--output-format streaming-json`, `--trust` on every launch, and the prompt as
`--prompt-file` (the run's prompt.txt). The `--sandbox` value is always explicit; `sandbox_for`
says which. Grok's configured `[sandbox] profile` is never read: a second
policy is what makes a dispatch from another harness refuse to start.
`--provider-arg` stays restrict-only.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

from ..lib.paths import git_toplevel
from .base import Event, Job, Provider, as_float, as_int, usage_tokens


_APPLY_YES = frozenset({"yes", "1", "true"})
_APPLY_NO = frozenset({"no", "0", "false"})
_profile_probe: bool | None = None


def profile_apply_blocked() -> bool:
    """True when `grok --sandbox read-only` would refuse to start.

    Under a parent sandbox grok cannot write
    `~/.grok/config.toml`, and grok exits rather than run without the profile.
    A grok tool-shell sets `GROK_AGENT=1`. Any other harness that already
    holds a sandbox hits the same refuse-start; a `--version` probe detects
    it. `DELEGATE_GROK_PROFILE_APPLY=yes|no` skips the probe so tests stay
    host-independent.
    """
    forced = os.environ.get("DELEGATE_GROK_PROFILE_APPLY", "").strip().lower()
    if forced in _APPLY_YES:
        return False
    if forced in _APPLY_NO:
        return True
    if os.environ.get("GROK_AGENT") == "1":
        return True
    return _profile_refused_cached()


def _profile_refused_cached() -> bool:
    global _profile_probe
    if _profile_probe is None:
        _profile_probe = _profile_refused("read-only")
    return _profile_probe


def _profile_refused(profile: str) -> bool:
    """True when grok exits because that profile cannot be applied."""
    try:
        proc = subprocess.run(
            ["grok", "--sandbox", profile, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    blob = f"{proc.stderr or ''}\n{proc.stdout or ''}".lower()
    return proc.returncode != 0 and (
        "could not apply" in blob or "operation not permitted" in blob
    )


def sandbox_for(job: Job) -> str:
    """The grok --sandbox value for this job.

    `--no-sandbox` selects `off`. `--read-only` selects the built-in
    `read-only` profile, or `off` when that profile cannot be applied in
    this process. An explicit `--provider-arg sandbox=NAME` pins one run.
    Everything else is built-in `workspace`. Grok's configured profile is
    not consulted.
    """
    if job.no_sandbox:
        return "off"
    if job.read_only:
        return "off" if profile_apply_blocked() else "read-only"
    extra = (job.extra.get("sandbox") or "").strip()
    if extra:
        return extra
    return "workspace"


def describe_sandbox() -> str:
    """One doctor line: which profile a default grok dispatch would use."""
    return "workspace (default)"


class Grok(Provider):
    name = "grok"
    runner = "grok"
    parses_stream = True
    read_only = "kernel"
    web_toggle = True
    answers = True
    supports = frozenset({"effort", "max_turns", "no_sandbox"})
    extra_keys = frozenset({"disallowed-tools", "deny", "no-memory", "sandbox"})
    prompt_via = "file"

    def argv(self, job: Job) -> list[str]:
        args = ["--cwd", str(job.cwd), "--permission-mode", "auto",
                "--no-plan", "--no-subagents",
                "--output-format", "streaming-json"]
        args += ["--sandbox", sandbox_for(job), "--trust"]
        if job.read_only and not job.web:
            args += ["--disable-web-search"]
        if job.extra.get("disallowed-tools"):
            args += ["--disallowed-tools", job.extra["disallowed-tools"]]
        if job.extra.get("deny"):
            args += ["--deny", job.extra["deny"]]
        if job.extra.get("no-memory", "").lower() in ("1", "true", "yes"):
            args += ["--no-memory"]
        if job.max_turns:
            args += ["--max-turns", str(job.max_turns)]
        if job.model:
            args += ["--model", job.model]
        if job.effort:
            args += ["--reasoning-effort", job.effort]
        args += ["--prompt-file", str(job.run_dir / "prompt.txt")]
        return args

    def doctor(self, cwd: Path | None) -> list[tuple[str, str]]:
        lines = [("ok", f"sandbox {describe_sandbox()}")]
        if cwd is not None:
            trusted, hint = saved_grant(cwd)
            lines.append(("ok", f"{cwd} trusted") if trusted else ("info", hint))
        cred = self.preflight_check(Job(self.name, cwd or Path("."), "", "doctor", cwd or Path(".")))
        lines.append(("FAIL", cred) if cred else ("ok", "credentials readable"))
        return lines

    def read_only_enforcement(self, job: Job) -> str | None:
        if job.read_only and sandbox_for(job) == "off":
            return "post-hoc"
        return super().read_only_enforcement(job)

    def validate(self, job: Job) -> str | None:
        extra = (job.extra.get("sandbox") or "").strip()
        if not extra:
            return None
        if job.no_sandbox:
            return "--provider-arg sandbox= and --no-sandbox contradict each other"
        if job.read_only:
            return "--provider-arg sandbox= and --read-only contradict each other"
        if extra == "off":
            return "--provider-arg sandbox=off is refused; pass --no-sandbox"
        return None

    def preflight_check(self, job: Job) -> str | None:
        # An API key is enough to start; the auth file is the other credential.
        if os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY"):
            return None
        auth = Path.home() / ".grok" / "auth.json"
        # Open directly. exists() re-raises EPERM and EACCES from stat.
        try:
            with auth.open("rb"):
                pass
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            detail = exc.strerror or exc.__class__.__name__
            return (
                f"grok cannot read its credentials {auth} ({detail}); "
                "the calling sandbox must allow reading it"
            )
        return None

    def events(self, data: dict) -> list[Event]:
        ev = _parse_acp(data)
        return [ev] if ev else []


def saved_grant(cwd: Path) -> tuple[bool, str]:
    """(trusted, hint) for grok's saved folder grant. Runs pass --trust and never need it."""
    target = cwd.resolve()
    top = git_toplevel(target) or target
    for folder in _up_to(target, top):
        if _read_trusted(str(folder)):
            return True, ""
    return False, (
        f"grok has not trusted {top}; runs pass --trust and do not need it. "
        f"To save a grant for interactive use: `cd {top} && grok`, approve the "
        f"folder-trust prompt, then quit (the grant covers its subdirectories)."
    )


def _up_to(start: Path, top: Path):
    """start, its parents, ..., top (inclusive); just start if top is not above it."""
    yield start
    if start == top or top not in start.parents:
        return
    for parent in start.parents:
        yield parent
        if parent == top:
            return


def _read_trusted(target: str) -> bool:
    trust_file = Path.home() / ".grok" / "trusted_folders.toml"
    if not trust_file.is_file():
        return False
    text = trust_file.read_text(encoding="utf-8", errors="replace")
    try:
        folders = tomllib.loads(text).get("folders")
    except tomllib.TOMLDecodeError:
        return False
    entry = folders.get(target) if isinstance(folders, dict) else None
    return isinstance(entry, dict) and entry.get("trusted") is True


# Grok emits ACP session updates as NDJSON. The exact envelope is verified
# against a recorded fixture in tests; this parser reads the common shapes
# tolerantly and never raises on an unexpected line (returns a best-effort
# Event or None). Shape assumptions live here so a grok CLI change is a
# one-file fix.
def _parse_acp(d: dict) -> Event | None:
    t = d.get("type") or d.get("sessionUpdate") or d.get("kind")
    if t == "error":
        return Event(kind="error", text=_error_text(d))
    # Terminal / result line.
    if t in ("result", "end", "turn_end", "session_end") or "stopReason" in d or "stop_reason" in d:
        return Event(
            kind="end",
            stop_reason=Provider._first(d, "stopReason", "stop_reason"),
            turns=as_int(Provider._first(d, "num_turns", "numTurns", "turns")),
            cost_usd=as_float(Provider._first(d, "total_cost_usd", "totalCostUsd", "cost_usd")),
        )
    # Reasoning chunk. Drop the text; live progress only needs the event.
    if t == "thought":
        return Event(kind="thought")
    # Per-request token deltas. Turns and cost still come only from an end event.
    if t == "usage":
        return Event(kind="usage", tokens=usage_tokens(d.get("usage"), _USAGE_FIELDS))
    # Tool call.
    if t in ("tool_call", "toolCall", "tool_use") or d.get("role") == "tool_call":
        name = Provider._first(d, "name", "tool", "toolName", "title")
        return Event(kind="tool_call", name=name, path=_tool_path(d),
                     write=_tool_writes(d, name))
    # Tool result / update.
    if t in ("tool_call_update", "tool_result", "toolResult"):
        status = Provider._first(d, "status", "state")
        ok = None
        if isinstance(status, str):
            ok = status.lower() not in ("failed", "error", "cancelled", "canceled")
        return Event(kind="tool_result",
                     name=Provider._first(d, "name", "tool", "toolName"), ok=ok)
    # Assistant text.
    if t in ("text", "message", "agent_message", "assistant"):
        return Event(kind="text", text=_text_of(d))
    return None


_WRITE_NAMES = frozenset({"write", "search_replace", "edit", "delete_file"})
_USAGE_FIELDS = (
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("reasoning_tokens", "reasoning"),
    ("cache_read_input_tokens", "cache_read"),
)


def _tool_writes(d: dict, name) -> bool:
    """True for grok kind "edit", or a tool whose name writes a file."""
    if d.get("kind") == "edit":
        return True
    return isinstance(name, str) and name in _WRITE_NAMES


def _tool_path(d: dict):
    inp = d.get("input") or d.get("arguments") or d.get("args") or d.get("rawInput") or {}
    if isinstance(inp, dict):
        return Provider._first(inp, "path", "file_path", "filename", "file", "target",
                               "target_file", "target_directory")
    return None


def _error_text(d: dict) -> str:
    for key in ("message", "error"):
        v = d.get(key)
        if isinstance(v, dict):
            v = v.get("message")
        if isinstance(v, str) and v:
            return v
    return "error"


def _text_of(d: dict):
    for k in ("text", "data", "content", "message"):
        v = d.get(k)
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            inner = v.get("text")
            if isinstance(inner, str):
                return inner
    return None
