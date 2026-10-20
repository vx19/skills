"""Provider contract.

A provider supplies runner-specific configuration and result interpretation:
  * how to build the runner's argv for a Job (the ONLY place a permission-mode
    or sandbox flag ever appears — never in the command the user types),
  * how to turn one line of the runner's stream into a neutral Event,
  * whether the target repo is trusted (when that is checkable offline).

Optional hooks prepare configuration, check the environment before launch,
ingest a final result, and release resources. Spawning, teeing,
snapshotting, and the envelope live in
`delegate.core` and is identical across providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Job:
    """A single delegation. Built by the CLI, consumed by core + provider."""

    provider: str
    cwd: Path
    prompt: str                       # fully assembled (rules + task + boilerplate)
    run_id: str
    run_dir: Path
    model: str | None = None
    effort: str | None = None
    max_turns: int | None = None
    scope: list[str] = field(default_factory=list)
    report: Path | None = None
    extra: dict[str, str] = field(default_factory=dict)  # --provider-arg K=V
    no_sandbox: bool = False
    dry_run: bool = False
    read_only: bool = False           # --read-only: the provider's no-write posture
    web: bool = False                 # --web: keep web tools under --read-only
    schema: dict | None = None        # --schema: required shape of the final answer
    computer_use: bool = False        # --computer-use: the worker may drive desktop apps


@dataclass
class Event:
    """One neutral event parsed from a provider stream line.

    `kind` is one of: tool_call, tool_result, text, cost, end, thought, usage,
    turn, error, other. `turn` counts a turn and prints nothing. `error` carries
    the provider's own failure message in `text`.
    A provider parser returns None for lines it does not care about.
    `tokens` is a usage delta (input, output, reasoning, cache_read).
    `write` is true when a tool call writes a file.
    """

    kind: str
    name: str | None = None          # tool name (tool_call / tool_result)
    path: str | None = None          # file path touched, if any
    ok: bool | None = None           # tool_result success
    text: str | None = None          # assistant text / final message
    cost_usd: float | None = None
    turns: int | None = None         # absolute turn count; last value wins
    turns_delta: int | None = None   # added to the running turn count
    stop_reason: str | None = None   # provider's own terminal reason, if given
    tokens: dict[str, int] | None = None  # usage delta: input, output, reasoning, cache_read
    write: bool = False              # tool_call writes a file

    def compact(self) -> str:
        """One human line for the streamed stderr view."""
        if self.kind == "tool_call":
            tgt = f" {self.path}" if self.path else ""
            return f"  → {self.name or 'tool'}{tgt}"
        if self.kind == "tool_result" and self.ok is False:
            return f"  ✗ {self.name or 'tool'} failed"
        if self.kind == "end":
            bits = []
            if self.turns is not None:
                bits.append(f"turns={self.turns}")
            if self.cost_usd is not None:
                bits.append(f"cost=${self.cost_usd:.4f}")
            if self.stop_reason:
                bits.append(self.stop_reason)
            if not bits:
                return ""
            return "  ● end " + " ".join(bits)
        return ""


def usage_tokens(src: dict, fields) -> dict[str, int] | None:
    """Map provider usage fields onto input/output/reasoning/cache_read ints."""
    if not isinstance(src, dict):
        return None
    tokens: dict[str, int] = {}
    for src_key, dst in fields:
        val = src.get(src_key)
        if isinstance(val, bool) or not isinstance(val, int):
            continue
        tokens[dst] = val
    return tokens or None


class Provider:
    """Base class. Subclasses set `name`/`runner` and implement `argv`/`parse`."""

    name: str = ""
    runner: str = ""
    # Whether the runner emits a machine-readable stream we can parse for
    # turns/cost/failed-tool-calls. Passthrough providers set this False and
    # core leaves those envelope fields null.
    parses_stream: bool = True
    # How --read-only is enforced: "kernel" (OS sandbox), "app" (the runner's own
    # no-edit mode), or None (refused: no mode that stops writes).
    read_only: str | None = None
    # Whether argv can switch web search/fetch off under --read-only.
    web_toggle: bool = False
    # Whether a final answer can be captured (stream parse or runner file).
    answers: bool = False
    # Whether the runner has a desktop-control tool for --computer-use.
    computer_use: bool = False
    # The --provider-arg keys this provider reads; any other key is refused (64)
    # rather than silently ignored.
    extra_keys: frozenset[str] = frozenset()

    def argv(self, job: Job) -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def command(self, job: Job) -> list[str]:
        """Complete launch command; providers may supply an explicit runner wrapper."""
        return [self.runner, *self.argv(job)]

    def parse(self, line: str) -> Event | None:  # pragma: no cover - default
        return None

    def trust(self, cwd: Path, job: Job | None = None) -> tuple[bool, str] | None:
        """Return (trusted, fix_hint) or None when trust is not checkable
        offline for this provider. core turns (False, hint) into exit 2.
        ``job`` is set on a real run. Grok passes ``--trust`` and returns
        None then, so a dispatch is not blocked on the saved grant. Doctor
        calls this without a job and still reports the file."""
        return None

    def preflight_check(self, job: Job) -> str | None:
        """Return an environment error, or None. core raises Preflight (exit 2)."""
        return None

    def validate(self, job: Job) -> str | None:
        """Return a usage-error message, or None if the job is well-formed."""
        return None

    def prepare(self, job: Job, env) -> None:
        """Prepare provider configuration; never spawn or snapshot here."""

    def answer_file(self, job: Job) -> Path | None:
        """A file the runner itself writes its final message to, if any.
        Otherwise core assembles the answer from parsed text events."""
        return None

    def environment(self, job: Job) -> dict[str, str]:
        return {}

    def computer_use_status(self) -> tuple[bool, str]:
        """(ready, detail) of the desktop-control tool, checked offline (doctor)."""
        return False, "no desktop-control tool"

    def finish(self, job: Job, env, returncode: int) -> None:
        """Add provider metadata/product outcome after core computes the delta."""

    def cleanup(self) -> None:
        """Release provider resources on every exit, including failed prepare."""

    # Small shared helpers for parsers.
    @staticmethod
    def _first(d: dict, *keys: str):
        for k in keys:
            v = d.get(k)
            if v not in (None, ""):
                return v
        return None
