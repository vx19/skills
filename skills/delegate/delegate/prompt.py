"""Assemble the prompt: rules files + task + a short boilerplate.

The boilerplate is deliberately tiny — four standing constraints. Three of
them stop the headless cancels (skills auto-firing, implicit questions, plan
mode). The fourth tells the worker to write to disk early, in small pieces.
Other guidance (reading budget, do-not-touch list, whole-file pytest) is
*content*, so it lives in the `templates/` files the user copies and edits.
Rules stay text, not code. HTML comments in a rules or task file are notes for
the orchestrator and are stripped.
"""

from __future__ import annotations

import re
from pathlib import Path

# Linux caps one argv string at 128 KiB (MAX_ARG_STRLEN). A runner that takes
# the prompt as one is refused a bigger one before any work, not with E2BIG.
MAX_PROMPT_BYTES = 120 * 1024

# Standing constraints baked into the prompt. Keep this short. A read-only
# run uses the variant below, which drops the write-early sentence.
BOILERPLATE = (
    "Operating constraints: do not invoke skills or a skill-router; do not ask "
    "the user questions (make a reasonable assumption and note it); do not enter "
    "plan mode — implement directly. Write work to disk early and in small "
    "pieces; prefer several small edits over one large single-pass write."
)
# --read-only replaces the plan-mode clause and omits the write-early sentence.
# The worker cannot write, so its final message is the deliverable (captured
# to `answerPath`).
READ_ONLY_BOILERPLATE = (
    "Operating constraints: do not invoke skills or a skill-router; do not ask "
    "the user questions (make a reasonable assumption and note it); this run is "
    "read-only — do not create, edit or delete files. Your final message is "
    "your deliverable."
)
# --computer-use appends this: the worker drives real apps on the user's Mac,
# so the limits that keep that safe ride in every such prompt.
COMPUTER_USE_CLAUSE = (
    "Computer use: you may drive desktop apps with your Computer Use tool, only "
    "the apps and screens this task needs. Do not enter passwords, codes or "
    "payment details, send messages, buy anything, or change settings unless the "
    "task explicitly says so. If the tool says it was not approved to use an "
    "app, stop and quote that message. Put everything you observed in your final "
    "message; it is your deliverable."
)


class PromptError(RuntimeError):
    pass


# Notes for the orchestrator in a rules or task file (template guidance, a
# filled example) are HTML comments; the worker never sees them.
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _read_file(p: Path) -> str:
    return _COMMENT.sub("", p.read_text(encoding="utf-8", errors="replace")).strip()


def read_task(task: str | None, task_is_file: bool) -> str:
    """`task` is either an inline string or a path (task_is_file)."""
    if task is None:
        raise PromptError("a task is required (a prompt argument or --task FILE)")
    if not task_is_file:
        if not task.strip():
            raise PromptError("task must not be empty")
        return task
    p = Path(task)
    if not p.is_file():
        raise PromptError(f"--task file not found: {task}")
    text = _read_file(p)
    if not text:
        raise PromptError(f"--task file is empty: {task}")
    return text


def read_rules(rules: list[str]) -> list[str]:
    out: list[str] = []
    for r in rules:
        p = Path(r)
        if not p.is_file():
            raise PromptError(f"--rules file not found: {r}")
        text = _read_file(p)
        if text:
            out.append(text)
    return out


def assemble(task: str, rules: list[str], *, read_only: bool = False,
             computer_use: bool = False) -> str:
    """rules... + task + boilerplate (+ computer-use clause) → one prompt string."""
    parts: list[str] = []
    for block in rules:
        parts.append(block)
    parts.append(task)
    parts.append(READ_ONLY_BOILERPLATE if read_only else BOILERPLATE)
    if computer_use:
        parts.append(COMPUTER_USE_CLAUSE)
    return "\n\n".join(p for p in parts if p.strip())


def check_argument_size(prompt: str, provider: str) -> None:
    size = len(prompt.encode("utf-8", errors="surrogateescape"))
    if size > MAX_PROMPT_BYTES:
        raise PromptError(f"the prompt is {size // 1024} KiB; {provider} takes it as one "
                          f"argument, capped at {MAX_PROMPT_BYTES // 1024} KiB. "
                          "Shorten --task or --rules, or use claude, codex or grok")
