"""Grok provider.

Launch posture: `--permission-mode auto`
(acceptEdits still prompts headlessly and the missing answer reads as cancel),
`--no-plan --no-subagents`, `--output-format streaming-json`, prompt via
`--single`. Every launch passes `--trust`, so a headless run from another
harness accepts the working tree without an interactive folder prompt.
`--read-only` uses grok's built-in `read-only` profile and adds
`--disable-web-search` unless `--web`. When this process is already
sandboxed, grok refuse-starts rather than apply that profile (it cannot
write its hooks registry). Delegate then passes `--sandbox off` and the
post-run delta check is what enforces `--read-only`. `--no-sandbox` passes
`--sandbox off`
explicitly, so a `GROK_SANDBOX` env cannot slip in.

The write sandbox is otherwise built-in `workspace` (writes only `--cwd`,
`~/.grok`, and temp). Delegate does not read `[sandbox] profile` from grok
config: a custom profile is a second policy, and that is what makes a
dispatch from another harness refuse to start. Pin one run with
`--provider-arg sandbox=NAME`. `sandbox=off` is refused.
`--provider-arg` otherwise stays restrict-only: `disallowed-tools=a,b`,
`deny=RULE`, `no-memory=true`.

`delegate doctor` still reports whether `~/.grok/trusted_folders.toml` grants
the repo, and whether `~/.grok/auth.json` can be read. A run does not fail on
the trust file: `--trust` is already on the argv. A grant covers the subdirectories of the same repository, not a nested
checkout, so the doctor check walks up from --cwd to the git top level.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from ..lib.paths import git_toplevel
from .base import Event, Job, Provider, usage_tokens

try:  # Python 3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

_TRUST_FILE = Path.home() / ".grok" / "trusted_folders.toml"
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


def read_only_inherit_warning(job: Job) -> str | None:
    """Stderr note when --read-only could not apply its kernel profile."""
    if not job.read_only or sandbox_for(job) != "off":
        return None
    return ("read-only profile cannot be applied in this process "
            "(the parent sandbox refuses it); using --sandbox off. "
            "--read-only is the post-run delta check")


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
    extra_keys = frozenset({"disallowed-tools", "deny", "no-memory", "sandbox"})

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
        args += ["--single", job.prompt]
        return args

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

    def trust(self, cwd: Path, job: Job | None = None) -> tuple[bool, str] | None:
        # A run passes --trust, so the saved grant must not exit 2 before grok
        # starts. Doctor calls this without a job and still reports the file.
        if job is not None:
            return None
        target = cwd.resolve()
        top = git_toplevel(target) or target
        for folder in _up_to(target, top):
            if _read_trusted(str(folder)):
                return True, ""
        hint = (
            f"grok has not trusted {top}. Trust the repository once: "
            f"`cd {top} && grok`, approve the folder-trust prompt, then quit "
            f"(the grant covers its subdirectories)."
        )
        return False, hint

    def parse(self, line: str) -> Event | None:
        line = line.strip()
        if not line or line[0] != "{":
            return None
        try:
            d = json.loads(line)
        except ValueError:
            return None
        return _parse_acp(d)


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
    if not _TRUST_FILE.is_file():
        return False
    text = _TRUST_FILE.read_text(encoding="utf-8", errors="replace")
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception:
            data = None
        if isinstance(data, dict):
            folders = data.get("folders")
            if isinstance(folders, dict):
                entry = folders.get(target)
                if isinstance(entry, dict) and entry.get("trusted") is True:
                    return True
                return False
    # Fallback: match `[folders."<target>"]` ... `trusted = true` without a parser.
    pat = re.compile(
        r'\[folders\."' + re.escape(target) + r'"\]([^\[]*)', re.S
    )
    m = pat.search(text)
    return bool(m and re.search(r"trusted\s*=\s*true", m.group(1)))


# Grok emits ACP session updates as NDJSON. The exact envelope is verified
# against a recorded fixture in tests; this parser reads the common shapes
# tolerantly and never raises on an unexpected line (returns a best-effort
# Event or None). Shape assumptions live here so a grok CLI change is a
# one-file fix.
def _parse_acp(d: dict) -> Event | None:
    t = d.get("type") or d.get("sessionUpdate") or d.get("kind")
    # Terminal / result line.
    if t in ("result", "end", "turn_end", "session_end") or "stopReason" in d or "stop_reason" in d:
        return Event(
            kind="end",
            stop_reason=Provider._first(d, "stopReason", "stop_reason"),
            turns=_int(Provider._first(d, "num_turns", "numTurns", "turns")),
            cost_usd=_float(Provider._first(d, "total_cost_usd", "totalCostUsd", "cost_usd")),
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
