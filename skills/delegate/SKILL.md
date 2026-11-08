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
`bin/delegate` and `templates/` are relative to this skill's folder. The paths
you pass (`--cwd`, `--rules`, `--task`, `--schema`, `--report`) resolve against
your own working directory; `--scope` resolves against `--cwd`.

```bash
bin/delegate run --provider grok --cwd /path/to/repo -- "one concrete task"
```

With standing rules and a per-phase task file (preferred for real work):

```bash
bin/delegate run --provider claude --cwd /path/to/repo \
  --rules my-rules.md \
  --task phase-1.task.md \
  --scope src/ --scope tests/ \
  --report /path/to/repo/.reports/phase-1.md
```

Copy the templates and fill them in: [`templates/rules.md`](templates/rules.md)
for standing rules, [`templates/task.md`](templates/task.md) for a change, and
[`templates/observe.task.md`](templates/observe.task.md) for a `--read-only`
question. HTML comments in a rules or task file are notes for you; the worker
never sees them. Prefer small, reviewable tasks.

Every runner starts in a non-prompting posture (`--permission-mode auto`,
`--approve-for-me`, `--force --trust`, …): a permission prompt nobody can
answer would read as a cancel.

`run` flags (`bin/delegate run --help` lists them):

| Flag | Meaning |
|------|---------|
| `--provider {grok,claude,codex,cursor,pi}` | Required. Which runner. |
| `--cwd DIR` | Target git work tree (default `.`). Must have a commit. |
| `--model M` | Pass-through model id. |
| `--effort E` | Reasoning effort, one lowercase word (else exit 64). Not forwarded by cursor (warns). |
| `--rules FILE` | Repeatable. Standing constraints, prepended to the prompt. |
| `--task FILE` | Task body from a file (mutually exclusive with a positional prompt). claude and codex read the prompt on stdin and grok from a file; cursor and pi take it as one argument, so for them it must stay under 120 KiB (exit 64). |
| positional / `-- PROMPT` | Inline task text (use `--` so flags in the prompt are not parsed). |
| `--max-turns N` | Turn cap (grok, claude; others warn). A fuse: hitting it fails the run (exit 1), not a partial success. |
| `--scope PATH` | Repeatable. Expected change paths, relative to `--cwd`. Violations are reported, never blocked. `.` / empty = all of `--cwd`. The `--report` file never counts. A live job with an overlapping scope in the same tree warns. |
| `--report FILE` | Adds one sentence to the prompt: write the report to FILE before the final verification run. A stub is written on a late cancel or timeout. |
| `--provider-arg K=V` | Repeatable. grok `sandbox=NAME` / `disallowed-tools=` / `deny=` / `no-memory=true`, pi `provider=`. Any other key exits 64. |
| `--background` | Detach; prints `{jobId, runDir}` and exits 0. A preflight failure prints the error envelope and exits 2 instead. Poll with `status`/`wait`. |
| `--no-sandbox` | Drop the provider sandbox (grok, codex, cursor; claude and pi have none and warn). Dangerous. |
| `--read-only` | Run with no writes; the deliverable is `answerPath`. Any delta fails exit 2, including one another writer made in the same tree. Refused (64) for pi and with `--no-sandbox` or `--report`. How each runner enforces it: `readOnlyEnforcement` below. |
| `--web` | Keep web search/fetch under `--read-only` (off by default on grok/claude; codex/cursor keep their default, with a warning). |
| `--schema FILE` | Final answer must be one JSON object matching this schema (any provider; appended to the prompt). Core takes the last top-level JSON object in the answer and checks every top-level `required` key. Success → `answer.json`. Mismatch → exit 1. |
| `--computer-use` | Let the worker drive desktop apps with Codex's Computer Use tool (codex only; refused with 64 elsewhere). See [Computer use](#computer-use-codex). |
| `--dry-run` | Print the exact `argv` and the assembled prompt (`providerExtra.argv`, `providerExtra.prompt`) and exit 0. Argv only: no preflight (a bad `--cwd` still exits 0) and no launch. With grok `--read-only` it runs `grok --version` once to choose the sandbox. |
| `--quiet` | Suppress the streamed per-event stderr view. |

Job control:

```bash
bin/delegate status [JOB] [--limit N]   # list or detail one job
bin/delegate wait JOB [--timeout S]     # block until it finishes
bin/delegate result JOB                 # print the finished envelope
bin/delegate logs JOB [--tail N] [--raw]  # compact view; --raw prints the JSONL
bin/delegate cancel JOB                 # request cancellation; core kills the worker group
                                        # (wrapper dead: stops the orphaned group, settles the run)
```

A run whose wrapper died before writing `result.json` converges to one synthetic
`error` envelope (`wrapper died before writing a result`) on the next `status`,
`wait`, or `result`, once its worker group is gone. That envelope carries the
worker's real delta (`diffPath`, `diffStat`, `emptyDiff`, `scopeViolations`)
against the run's baseline. Run dirs are kept until you opt in to cleanup: with
`DELEGATE_KEEP_DAYS=N`, `run` deletes finished run dirs older than N days, at
most once a day.

Setup:

```bash
bin/delegate doctor [--provider P] [--cwd DIR]   # runners, git, trust
bin/delegate --version
```

## Exit codes

| Exit | Meaning |
|------|---------|
| `0` | Success (`stopReason: end_turn`); `doctor`: a run could start. |
| `1` | Worker failure (`error`), `cancelled` or `timeout`. Also an unknown job, or a dead wrapper with no result, for `status`/`wait`/`result`/`cancel`. |
| `2` | Preflight or wrapper error, with an `error` envelope: runner missing, bad `--cwd`, snapshot or diff failure, a write under `--read-only`, a git-metadata change, a moved HEAD. `doctor`: a run could not start. |
| `3` | `wait --timeout` ended while the job is still running. |
| `64` | Usage error (bad flag, oversized prompt, invalid job id). **No envelope**, only a stderr line. |
| `130` | Interrupted by Ctrl-C outside a run (a running job's SIGINT ends it as `cancelled`, exit 1). |

So `stopReason: error` carries exit 1 when the worker failed and exit 2 when
the wrapper did. `doctor` exits 2 for a provider named with `--provider` whose
runner is missing, unreadable grok credentials, or a `--cwd` that is not a git
work tree with a commit. Without `--provider`, a missing runner is only a
`warn`. `info` lines never change the exit code.

## Watching a live run

Poll `status`, or read `progress.json` in the run dir. Do not judge a run by
`git diff`. A healthy worker can reason for minutes and then write in a burst.

```text
running 14m12s · last thought 2s ago · last tool 11m00s ago · last write 3m05s ago · tools 9 (writes 2, failed 1) · tokens out 36.1k (reasoning 36.0k) · heartbeat 1s ago
```

- `last thought` is the kind and age of the last parsed event (`last event` when the kind was not recorded).
- `last tool` / `last write` stay `never` until one happens.
- `tokens out` appears once `usage.requests` is at least 1.
- `heartbeat never` means that file is missing. The `heartbeat` file is deprecated
  and goes at 1.0; `progress.json`'s `updatedAt` carries the same signal.

Judge grok, codex and pi by `usage.requests` and output tokens rising
(`tokens out`, or `usage` in `progress.json`). Claude and cursor report no
tokens: judge them by the age of the last event and the heartbeat. A healthy
Grok worker often makes no write for 5–8 minutes, including 2+ minutes with no
tool call, then writes in a burst. Do not cancel early unless that signal has
stopped moving for several minutes.

Delegate itself ends a run only after 20 minutes with no runner output at all
(stdout or `runner.log` growth), or at the 4-hour hard cap; both take a
seconds override in `DELEGATE_IDLE_TIMEOUT` / `DELEGATE_HARD_CAP`. `wait
--timeout` does not change them.

The state directory is private (mode 0700): run dirs hold the prompt, the full
transcript and provider args. A real run creates it that way, and every command
tightens one an older version left open. Files the worker writes in your
repository keep your umask.

## Grok sandbox

Delegate always passes grok an explicit `--sandbox` value (`GROK_SANDBOX` cannot
slip in) and always passes `--trust`, so a run never waits on
`trusted_folders.toml`; `doctor` reports a saved grant as `info`. The sandbox
value is:

1. `off` under `--no-sandbox`
2. `read-only` under `--read-only`. If this process is already sandboxed,
   grok refuse-starts; delegate then passes `off` and `--read-only` is the
   post-run delta check (a changed tree still exits 2), recorded as
   `readOnlyEnforcement: "post-hoc"`. Web stays off unless `--web`.
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
  A runner that exits 0 but whose stream ends on an error (claude `is_error` or
  an `error_*` subtype, a grok `error` event, a pi `stopReason: error` message,
  a cursor `is_error` result) is `error`, exit 1, with no `answerPath`.
  A run that changes `.git/config`, hooks, `info/attributes`, `info/exclude`, the
  checked-out branch or a local branch or tag is `error`, exit 2: the wrapper reports it
  and never repairs it. Its own git calls ignore the repo's fsmonitor, hooks, external
  diff and filter settings, so a diff shows raw file bytes.
- `exitCode` — see [Exit codes](#exit-codes).
- `error` — the runner's own failure message when its stream reports one;
  otherwise the stderr or preflight reason.
- `errorKind` — `null` unless `error` is set; then `usage_limit` or `auth` (the
  API's HTTP 429, or 401/403), `max_turns`, `timeout`, or `other`. It is read
  from structured fields, never from the worker's text; the one prose match is
  codex's own usage-limit message, which has no status. Other limits a runner
  reports only in prose are `other`. On `usage_limit`, switch provider or wait
  until `resetsAt` (UTC, when the runner gives one; claude does); delegate
  never retries. Before any rerun, read a non-empty `diffPath`: the
  next run's baseline includes those writes, so its delta will not show them.
- `diffPath` / `diffStat` / `emptyDiff` — the **authoritative worker-delta**
  (a private snapshot before vs after; pre-existing dirt is excluded). The diff
  is never inlined; open `diffPath` and read the whole thing. Snapshots cover
  tracked and unignored new files, inside checked-out submodules too. Ignored
  files, paths outside `--cwd` and a submodule's own `.git` are invisible;
  unstat-able paths are skipped and named on stderr.
- `baseCommit` / `dirtyBefore` — HEAD when the run started, and whether the tree
  already had changes then (they are not in the delta). A moved HEAD, empty
  commits included, fails exit 2: the wrapper never commits.
- `scope` / `scopeViolations` — paths that fell outside `--scope`. Violations
  are relative to the repository root, like the paths in `diffPath`.
- `reportPath` / `reportExists` — the report contract.
- `answerPath` — the worker's final message (`null` when none was captured).
  `readOnly` echoes `--read-only`. `readOnlyEnforcement` says what stopped
  writes: `kernel` (grok's and codex's OS sandbox), `app` (claude's and
  cursor's plan mode), `post-hoc` (only the delta check), or `null` without
  `--read-only`.
- `turns` / `costUsd` — `turns` is populated for grok, claude, codex, and pi;
  `costUsd` for grok, claude, and pi. Both are `null` for cursor.
  `failedToolCalls` counts tool calls that reported failure;
  `durationSeconds` is the wall time.
- `providerExtra` — provider-only fields (a raw status, usage tokens, etc.).
  `providerExtra.usage` (grok, codex, and pi) holds input, output, reasoning,
  and cache-read tokens plus `requests`. It is kept on cancel and timeout.
  `providerExtra.maxIdleSeconds` is the run's longest silence, so the idle
  limit can be tuned from real runs.

State lives under `~/.local/state/delegate/<runId>/` (`$XDG_STATE_HOME`
respected): `result.json`, `stream.jsonl`, `runner.log`,
`diff.patch`, `heartbeat`, `progress.json`, snapshot indexes, `command.json`
(argv, prompt elided), `prompt.txt`. Diagnose a failed run from those files.

## Outcome and containment contract

- One attempt. The wrapper never commits, never retries and never repairs.
- An empty diff is success.
- `cancel`, SIGINT, and SIGTERM all clean up and emit `cancelled`.
- SIGKILL cannot emit an envelope.

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
   headless run cannot answer, so an app works only once you have chosen
   **"Always allow"** for it in an interactive Codex session (`codex`, then ask
   it to open the app).
3. **A refusal fails the run** (exit 1): `error` names the app and the fix,
   and `providerExtra.computerUseDenied` lists the refused apps.

Write the task as what to look at and what to report; name the apps. The flag
adds a standing clause: only the apps the task needs; no credentials, messages,
purchases or settings changes unless asked. `--read-only` stops file writes, not
clicks.
