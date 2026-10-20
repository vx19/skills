---
name: delegate
description: >
  Delegate a coding task to a headless provider agent. One CLI wraps five
  runners — grok, claude, codex, cursor, and pi — behind
  `delegate run --provider ...`. Use for: delegate,
  "dispatch to grok / claude / codex / cursor / pi",
  "delegate this task to <provider>", "run a headless coding agent",
  "run this non-interactively", background/one-shot implementation while you
  plan and review. Also computer use via codex (`--computer-use`): "use codex
  to control an iPhone / a Mac app", "read this off a phone screen",
  "delegate computer use", iPhone Mirroring, screenshots of an app.
version: "0.5.0"
license: MIT
---

# delegate

`delegate` dispatches one coding task to a headless provider agent, waits for a
structured result, and hands you an authoritative diff to review. You are the
**orchestrator**. The provider is a **worker**: one task, same working tree, no
commit ownership, no built-in review.

## Run contract

Live runs currently require a POSIX host for process groups and job control.
Paths such as `bin/delegate` and `templates/` are relative to this skill's folder.

```bash
bin/delegate run --provider grok --cwd /path/to/repo -- "one concrete task"
```

With standing rules and a per-phase task file (preferred for real work):

```bash
bin/delegate run --provider claude --cwd /path/to/repo \
  --rules templates/rules.md \
  --task phase-1.task.md \
  --scope src/ --scope tests/ \
  --report /path/to/repo/.reports/phase-1.md
```

`run` flags (`bin/delegate run --help` lists them):

| Flag | Meaning |
|------|---------|
| `--provider {grok,claude,codex,cursor,pi}` | Required. Which runner. |
| `--cwd DIR` | Target git work tree (default `.`). Must have a commit. |
| `--model M` | Pass-through model id. |
| `--effort E` | Reasoning effort. **Not** forwarded by cursor; adapter support varies. |
| `--rules FILE` | Repeatable. Standing constraints, prepended to the prompt. |
| `--task FILE` | Task body from a file (mutually exclusive with a positional prompt). |
| positional / `-- PROMPT` | Inline task text (use `--` so flags in the prompt are not parsed). |
| `--max-turns N` | Turn cap (grok, claude). A fuse: hitting it fails exit 1 ("max turns reached"), not a partial success. |
| `--scope PATH` | Repeatable. Expected change paths; violations reported, not blocked. `.` / empty = whole tree. |
| `--report FILE` | Path added to the prompt; a stub is synthesised on a late cancel/timeout. |
| `--provider-arg K=V` | Repeatable. grok `sandbox=NAME` / `disallowed-tools=` / `deny=` / `no-memory=true`, pi `provider=`. |
| `--background` | Detach; prints `{jobId, runDir}`; poll with `status`/`wait`. |
| `--no-sandbox` | Drop the provider sandbox (grok/codex/cursor). Dangerous. See [Grok sandbox](#grok-sandbox). |
| `--read-only` | Runner no-write mode (grok, codex: kernel; claude, cursor: plan). Any delta fails exit 2. Deliverable is `answerPath`. Other writers in the same tree also trip it. Refused (64) for pi, `--no-sandbox`, `--report`. |
| `--web` | Keep web search/fetch under `--read-only` (off by default on grok/claude; codex/cursor keep their default, with a warning). |
| `--schema FILE` | Final answer must be one JSON object matching this schema (prompt-appended; core takes the whole answer, else last fence, else `{…}` span, and checks every top-level `required` key). Success → `answer.json`. Mismatch → exit 1. Accepted for pi. |
| `--computer-use` | Let the worker drive desktop apps with Codex's Computer Use tool (codex only; refused with 64 elsewhere). See [Computer use](#computer-use-codex). |
| `--dry-run` | Print the exact `argv` in the envelope; no launch, offline. |
| `--quiet` | Suppress the streamed per-event stderr view. |

Job control:

```bash
bin/delegate status [JOB] [--limit N]   # list or detail one job
bin/delegate wait JOB [--timeout S]     # block until it finishes (exit 3 if still live)
bin/delegate result JOB                 # print the finished envelope
bin/delegate logs JOB [--tail N] [--raw]  # compact view; --raw prints the JSONL
bin/delegate cancel JOB                 # request cancellation; core kills the worker group
```

A run whose wrapper died before writing `result.json` converges to one synthetic
`error` envelope (`wrapper died before writing a result`) on the next `status`,
`wait`, or `result`. Run dirs are kept until you opt in to cleanup: with
`DELEGATE_KEEP_DAYS=N`, `run` deletes finished run dirs older than N days, at
most once a day.

Setup:

```bash
bin/delegate doctor [--provider P] [--cwd DIR]   # runners, git, trust
bin/delegate --version
```

## Watching a live run

Poll `status`, or read `progress.json` in the run dir. Do not judge a run by
`git diff`. A healthy worker can reason for minutes and then write in a burst.

```text
running 14m12s · last thought 2s ago · last tool 11m00s ago · last write 3m05s ago · tools 9 (writes 2, failed 1) · tokens out 36.1k (reasoning 36.0k) · heartbeat 1s ago
```

- `last thought` is the kind and age of the last parsed event (`last event` when the kind was not recorded).
- `last tool` / `last write` stay `never` until one happens.
- `tokens out` appears once `usage.requests` is at least 1.
- `heartbeat never` means that file is missing.

`wait JOB --timeout S` returns the envelope's exit code (0 success, 1 worker
failure, 2 preflight or lifecycle error). **3 means it is still running**.
Exit 1 is also an unknown job, or a dead wrapper that has just received its synthetic error envelope.

Judge the run by `usage.requests` and output tokens rising (`tokens out` on
the status line, or `usage` in `progress.json`). A healthy Grok worker often
makes no write for 5–8 minutes, including 2+ minutes with no tool call, then
writes in a burst. Do not cancel early unless that token count has stopped
moving for several minutes. Delegate itself ends a run only after 20 minutes
with no runner output at all (stdout or `runner.log` growth), or at the
4-hour hard cap; both limits take a seconds override in
`DELEGATE_IDLE_TIMEOUT` / `DELEGATE_HARD_CAP`.

## Grok sandbox

Delegate always passes grok an explicit `--sandbox` value (`GROK_SANDBOX` cannot
slip in) and always passes `--trust`. The sandbox value is:

1. `off` under `--no-sandbox`
2. `read-only` under `--read-only`. If this process is already sandboxed,
   grok refuse-starts; delegate then passes `off` and `--read-only` is the
   post-run delta check (a changed tree still exits 2). Web stays off unless `--web`.
3. `--provider-arg sandbox=NAME` when given (`sandbox=off` is refused)
4. else grok's built-in `workspace` (writes only `--cwd` + `~/.grok` + temp)

Delegate does not follow `[sandbox] profile` in `~/.grok/config.toml` (a second
policy; applying it from another harness refuse-starts the worker).
`delegate doctor --provider grok` prints `workspace (default)`. It FAILs when
`~/.grok/auth.json` exists but cannot be read, unless `XAI_API_KEY` or
`GROK_API_KEY` is set; a run exits 2 for the same reason.

## Pi

Pi has no sandbox of its own. Before launch, Pi checks that its config dir can
take a lock (`$PI_CODING_AGENT_DIR` or `~/.pi/agent`). A missing dir is left
alone; an unwritable existing dir exits 2.

## The envelope

Every run writes one schema-3 JSON object to stdout and `result.json`
(`delegate/envelope.py`). Read these first:

- `stopReason` — one of `end_turn`, `cancelled`, `timeout`, `error`.
- `exitCode` — 0 success, 1 worker failure/cancel/timeout, 2 preflight/error.
- `error` — the runner's own failure message when its stream reports one;
  otherwise the stderr or preflight reason.
- `diffPath` / `diffStat` / `emptyDiff` — the **authoritative worker-delta**
  (a private snapshot before vs after; pre-existing dirt is excluded). The diff
  is never inlined; open `diffPath` and read the whole thing.
- `scope` / `scopeViolations` — paths that fell outside `--scope`.
- `reportPath` / `reportExists` — the report contract.
- `answerPath` — the worker's final message (`null` when none was captured).
  `readOnly` echoes `--read-only`.
- `turns` / `costUsd` — `turns` is populated for grok, claude, codex, and pi;
  `costUsd` for grok, claude, and pi. Both are `null` for cursor.
- `providerExtra` — provider-only fields (a raw status, usage tokens, etc.).
  `providerExtra.usage` (grok, codex, and pi) holds input, output, reasoning,
  and cache-read tokens plus `requests`. It is kept on cancel and timeout.
  `providerExtra.maxIdleSeconds` is the run's longest silence, so the idle
  limit can be tuned from real runs.

State lives under `~/.local/state/delegate/<runId>/` (`$XDG_STATE_HOME`
respected): `result.json`, `stream.jsonl`, `runner.log`,
`diff.patch`, `heartbeat`, `progress.json`, snapshot indexes, `command.json`
(argv, prompt elided), `prompt.txt`. Diagnose a failed run from those files.

## Important operating behavior

1. **Runs never prompt.** Each provider's argv sets a non-prompting posture
   (`--permission-mode auto`, `--approve-for-me`, `--force --trust`, …). An
   unanswered permission prompt would read as a cancel.

2. **Grok always gets `--trust`.** A run is not blocked on
   `trusted_folders.toml`. `doctor` still reports the saved grant (a grant
   covers its subdirectories, not a nested checkout).

3. **Use the templates for multi-step work.** Copy and adapt
   [`templates/rules.md`](templates/rules.md) and
   [`templates/task.md`](templates/task.md), then pass them with `--rules` and
   `--task`. Prefer small, reviewable tasks.

## Outcome and containment contract

- One attempt. The wrapper never commits.
- Moved HEAD (empty commits included) or snapshot failure → exit 2.
- Empty diff is success.
- Snapshots cover tracked + unignored new files. Ignored writes are invisible;
  unstat-able paths are skipped and named on stderr.
- `--scope` reports; it does not block. `.` / empty means the whole tree.
- `cancel`, SIGINT, and SIGTERM all clean up and emit `cancelled`.
- SIGKILL cannot emit an envelope.
- `wait --timeout` does not change the worker limits (20 min idle, 4 h hard cap).
- Unsupported effort and turn-cap flags warn.

## Computer use (codex)

Delegate desktop control (iPhone Mirroring, a Mac app) to codex's Computer Use
plugin. For observation, pair with `--read-only` and `--schema`:

```bash
bin/delegate run --provider codex --computer-use --read-only \
  --cwd /path/to/repo --schema values.schema.json --task read-phone.task.md
```

1. **Check the plugin:** `bin/delegate doctor --provider codex` prints
   `computer use: cua_repl plugin …` when Codex Desktop has installed it.
2. **App approval stays with the user.** Computer Use asks per app, and a
   headless run cannot answer, so an app works only once it has been approved:
   - **"Always allow"** in an interactive Codex session (`codex`, then ask it to
     open the app) → plain runs work from then on.
3. **A refusal fails the run** (exit 1): `error` names the app and the fix,
   and `providerExtra.computerUseDenied` lists the refused apps.

Write the task as what to look at and what to report; name the apps. The flag
adds a standing clause: only the apps the task needs; no credentials, messages,
purchases or settings changes unless asked. `--read-only` stops file writes, not
clicks.
