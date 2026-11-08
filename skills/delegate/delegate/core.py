"""Run lifecycle: preflight → spawn → stream → post-run → envelope.

Identical for every provider. The provider supplies a command + a stream parser +
an optional trust check; core does everything else, so adding a provider never
touches this file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .answer import check as check_answer
from .envelope import (
    Envelope,
    STOP_CANCELLED,
    STOP_END,
    STOP_TIMEOUT,
)
from .jobs import _fmt_duration, group_alive, read_job
from .lib import gitguard
from .lib.paths import git_dirty, git_head, git_ok, git_toplevel
from .lib.policy import violations
from .lib.snapshot import diff_from_tree, diff_paths, snapshot_tree, submodule_diff
from .providers.base import Event, Job, Provider

IDLE_TIMEOUT = 1200    # seconds of runner silence before the run ends
HARD_CAP = 14400       # seconds; a runaway guard, not a turn cap
_PROGRESS_EVERY = 2.0  # seconds between live progress.json updates


def _env_seconds(name: str, default: float) -> float:
    """Read a positive number of seconds from the environment."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class Preflight(Exception):
    """Environment/trust failure → exit 2 with a one-line message."""


def preflight(job: Job, provider: Provider) -> None:
    try:
        runner = provider.command(job)[0]
    except ValueError as e:
        raise Preflight(str(e)) from None
    if shutil.which(runner) is None:
        raise Preflight(f"{runner} is not executable" if Path(runner).is_absolute()
                        else f"{runner} is not on PATH")
    if not job.cwd.is_dir():
        raise Preflight(f"--cwd is not a directory: {job.cwd}")
    if not git_ok(job.cwd):
        raise Preflight(f"--cwd is not a git work tree: {job.cwd}")
    if git_head(job.cwd) is None:
        raise Preflight(f"git repo has no commit yet: {job.cwd}")
    blocked = provider.preflight_check(job)
    if blocked:
        raise Preflight(blocked)


_PROGRESS_KINDS = frozenset({"text", "tool_call", "tool_result", "thought", "usage"})


class _Accum:
    """Rolling tally built from parsed stream events."""

    def __init__(self) -> None:
        self.turns: int | None = None
        self.cost_usd: float | None = None
        self.failed_tool_calls = 0
        self.stop_reason: str | None = None
        # The runner's latest error and its kind, until later progress shows it recovered.
        self.error: str | None = None
        self.error_kind: str | None = None
        self.resets_at: int | None = None
        self.last_texts: list[str] = []
        # The final message: text since the last tool activity (grok streams it
        # in chunks), unless the runner's end event carries the whole answer.
        self.message: list[str] = []
        self.final_text: str | None = None
        self.counts = {"tool_call": 0, "write": 0, "failed_tool": 0}
        self.usage = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0,
                      "requests": 0}
        self.last_kind = None
        self.last_event_at = None
        self.last_tool_at = None
        self.last_write_at = None

    def add(self, ev: Event) -> None:
        now = time.time()
        self.last_kind = ev.kind
        self.last_event_at = now
        if ev.kind == "tool_call":
            self.counts["tool_call"] += 1
            if ev.write:
                self.counts["write"] += 1
                self.last_write_at = now
        if ev.kind == "usage":
            self.usage["requests"] += 1
            for key in ("input", "output", "reasoning", "cache_read"):
                val = (ev.tokens or {}).get(key)
                if val is not None:
                    self.usage[key] += val
        if ev.kind in ("tool_call", "tool_result"):
            self.last_tool_at = now
        if ev.turns is not None:
            self.turns = ev.turns
        if ev.turns_delta:
            self.turns = (self.turns or 0) + ev.turns_delta
        # Usage cost sums (pi message_end). Any other cost replaces it.
        if ev.kind == "usage" and ev.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + ev.cost_usd
        elif ev.cost_usd is not None:
            self.cost_usd = ev.cost_usd
        if ev.stop_reason:
            self.stop_reason = ev.stop_reason
        if ev.kind == "limit":
            self.resets_at = ev.resets_at
        if ev.kind == "error":
            self.error, self.error_kind = ev.text or "error", ev.error_kind
        elif ev.kind in _PROGRESS_KINDS:
            # A runner that retries (codex "Reconnecting… 1/5") and then makes
            # progress has recovered; only an error at the end of the stream counts.
            self.error = self.error_kind = None
        if ev.kind == "tool_result" and ev.ok is False:
            self.failed_tool_calls += 1
            self.counts["failed_tool"] += 1
        if ev.kind in ("tool_call", "tool_result"):
            self.message = []
        if ev.kind == "text" and ev.text:
            self.message.append(ev.text)
        if ev.kind == "end" and ev.text:
            self.final_text = ev.text
        if ev.kind == "text" and ev.text:
            self.last_texts.append(ev.text)
            if len(self.last_texts) > 5:
                self.last_texts.pop(0)

    def answer(self) -> str | None:
        if self.final_text is not None:
            return self.final_text
        return "".join(self.message) or None


def _record_answer(job: Job, provider: Provider, env: Envelope, acc: _Accum) -> str | None:
    """Point answerPath at the final message; return its text."""
    if acc.error:
        return None
    runner_file = provider.answer_file(job)
    if runner_file is not None:
        if runner_file.is_file():
            text = runner_file.read_text(encoding="utf-8", errors="replace")
            env.answer_path = str(runner_file)
            return text
        return None
    text = acc.answer()
    if text:
        path = job.run_dir / "answer.txt"
        path.write_text(text, encoding="utf-8")
        env.answer_path = str(path)
    return text


def envelope_for(job: Job, provider: Provider) -> Envelope:
    """A fresh envelope carrying the job's own fields, before any outcome."""
    env = Envelope(provider=provider.name, run_id=job.run_id, cwd=str(job.cwd))
    env.model, env.scope, env.dry_run = job.model, list(job.scope), job.dry_run
    env.report_path = str(job.report) if job.report else None
    env.read_only = job.read_only
    env.read_only_enforcement = provider.read_only_enforcement(job)
    return env


def measure_delta(cwd: Path, run_dir: Path, tree0: str, scope: list[str],
                  report: Path | None, env: Envelope) -> None:
    """Record the worker-delta against baseline `tree0`: diff, stat, scope.

    One rule for every run, whether its wrapper finished it or a later
    `settle_dead` found it dead. Diff paths are relative to the repository
    root and `--scope` to `--cwd`, so the scope is rebased onto the root; the
    report the caller asked for is never a violation.
    """
    index0, index1 = run_dir / "index0", run_dir / "index1"
    diff_text = diff_from_tree(cwd, tree0, index1) + submodule_diff(cwd, index0, index1)
    env.diff_stat, env.empty_diff = _stat_from_diff(diff_text)
    if diff_text:
        diff_path = run_dir / "diff.patch"
        diff_path.write_bytes(diff_text.encode("utf-8", errors="surrogateescape"))
        env.diff_path = str(diff_path)
    if scope:
        top = git_toplevel(cwd)
        allow = [os.path.relpath(cwd.resolve() / s, top) for s in scope]
        if report is not None:
            allow.append(os.path.relpath(report.resolve(), top))
        env.scope_violations = violations(diff_paths(diff_text), allow)


def _spawn(argv: list[str], cwd: Path, log_path: Path, extra_env: dict[str, str],
           stdin: Path | None) -> subprocess.Popen:
    with log_path.open("w", encoding="utf-8", errors="replace") as log, \
         (stdin.open("rb") if stdin else open(os.devnull, "rb")) as feed:
        return subprocess.Popen(
            argv, cwd=str(cwd), stdin=feed,
            # A runner may print bytes that are not UTF-8; one must not end the run.
            stdout=subprocess.PIPE, stderr=log, encoding="utf-8", errors="replace", bufsize=1,
            env={**os.environ, **extra_env}, start_new_session=True,
        )


class _Liveness:
    """Wall-clock silence meter shared by the stdout reader and the watch loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = time.monotonic()
        self.max_idle = 0.0

    def touch(self) -> None:
        with self._lock:
            self._record(time.monotonic())

    def idle_for(self, now: float | None = None) -> float:
        with self._lock:
            return (time.monotonic() if now is None else now) - self._last

    def finish(self) -> None:
        """Count the final gap, up to now, as silence."""
        with self._lock:
            self._record(time.monotonic())

    def _record(self, now: float) -> None:
        gap = now - self._last
        if gap > self.max_idle:
            self.max_idle = gap
        self._last = now


def _write_progress(run_dir: Path, acc: _Accum,
                    liveness: _Liveness | None = None) -> None:
    """Atomically refresh `<run_dir>/progress.json`. An OSError never escapes."""
    dest = run_dir / "progress.json"
    tmp = run_dir / "progress.json.tmp"
    try:
        tmp.write_text(json.dumps({
            "updatedAt": time.time(),
            "lastKind": acc.last_kind,
            "lastEventAt": acc.last_event_at,
            "lastToolAt": acc.last_tool_at,
            "lastWriteAt": acc.last_write_at,
            "counts": dict(acc.counts),
            "usage": dict(acc.usage),
            "maxIdleSeconds": int(liveness.max_idle if liveness is not None else 0),
        }, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _stream(proc: subprocess.Popen, provider: Provider, stream_path: Path,
            heartbeat: Path | None, acc: _Accum, quiet: bool, run_dir: Path,
            liveness: _Liveness) -> None:
    raw = stream_path.open("w", encoding="utf-8", errors="replace")
    last_progress = None
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            raw.write(line)
            raw.flush()
            liveness.touch()
            if heartbeat is not None:
                try:
                    heartbeat.touch()
                except OSError:
                    pass
            if provider.parses_stream:
                for ev in provider.parse(line):
                    acc.add(ev)
                    if not quiet:
                        msg = ev.compact()
                        if msg:
                            print(msg, file=sys.stderr, flush=True)
            else:
                # No events to parse; the line itself is the sign of life.
                acc.last_event_at = time.time()
            now = time.monotonic()
            if last_progress is None or now - last_progress >= _PROGRESS_EVERY:
                _write_progress(run_dir, acc, liveness)
                last_progress = now
    finally:
        try:
            raw.close()
        finally:
            # Close can fail; the final snapshot still has to land.
            _write_progress(run_dir, acc, liveness)


class _WorkerGroup:
    """The worker's process group, which the leader's pid names.

    Once the group is seen empty with its leader reaped, the id is free for
    the kernel to give another process, so the group is never signalled again.
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.gone = False

    def kill(self, *, grace: float = 1.0) -> None:
        """Tear down the whole group, not just its leader.

        The leader may exit — cleanly and successfully — while a backgrounded
        descendant keeps running and keeps writing into the work tree. Those
        writes would land after the final snapshot, outside the reported delta,
        so the group is always emptied before the delta is taken. An already-empty
        group raises ProcessLookupError on the first signal, which costs nothing on
        the common clean exit.
        """
        if self.gone:
            return
        proc = self.proc
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.wait()  # no group, so the leader has exited; reap it
            self.gone = True
            return
        deadline = time.monotonic() + grace
        try:
            proc.wait(timeout=grace)  # reap the leader so it stops holding the group
        except subprocess.TimeoutExpired:
            pass
        while time.monotonic() < deadline and group_alive(proc.pid):
            time.sleep(0.02)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        self.gone = not group_alive(proc.pid)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_REASON_LIMIT = 300
# Tracing (`2026-01-01T00:00:00.000000Z  WARN ...`) and warn/warning lines
# are not a runner failure reason. `error=` inside a trace is not `error:`.
_RUNNER_NOISE = re.compile(
    r"^(?:"
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s+"
    r"(?:WARN|INFO|DEBUG|TRACE)\b"
    r"|warn(?:ing)?\b"
    r"|reading additional input from stdin\b)",
    re.IGNORECASE,
)


def _runner_error(log_path: Path) -> str | None:
    """Best stderr reason for a failed runner, at most 300 characters.

    The last `error:` line (any case) wins, and only the text after the colon
    is kept — grok writes lowercase `error:`. Otherwise the last non-empty
    line that is not log noise. None when every line is blank or noise.
    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fallback = None
    for line in reversed(text.splitlines()):
        line = _ANSI.sub("", line).strip()
        if not line:
            continue
        if line.lower().startswith("error:"):
            reason = line.split(":", 1)[1].strip()
            return reason[:_REASON_LIMIT] or None
        if fallback is None and not _RUNNER_NOISE.match(line):
            fallback = line[:_REASON_LIMIT]
    return fallback


def _record_command(job: Job, argv: list[str]) -> None:
    """Keep the exact launch for diagnosis: argv (prompt elided) and the prompt."""
    elided = [f"<prompt: {len(job.prompt)} chars, prompt.txt>" if a == job.prompt else a
              for a in argv]
    (job.run_dir / "prompt.txt").write_text(job.prompt, encoding="utf-8")
    (job.run_dir / "command.json").write_text(
        json.dumps({"argv": elided, "cwd": str(job.cwd)}, indent=2) + "\n",
        encoding="utf-8")


def _stat_from_diff(diff_text: str) -> tuple[str, bool]:
    """Summarise a unified diff: 'N files changed, +A/-D'. Returns (stat, empty)."""
    if not diff_text.strip():
        return "", True
    files = set()
    add = rem = 0
    in_hunk = False  # `---`/`+++` headers precede a file's first hunk
    for ln in diff_text.splitlines():
        if ln.startswith("diff --git"):
            files.add(ln)
            in_hunk = False
        elif ln.startswith("@@"):
            in_hunk = True
        elif in_hunk and ln.startswith("+"):
            add += 1
        elif in_hunk and ln.startswith("-"):
            rem += 1
    n = len(files)
    return f"{n} file(s) changed, +{add}/-{rem}", n == 0


def _write_stub_report(report: Path, env: Envelope, acc: _Accum) -> None:
    """On a late cancel/timeout the worker may not have written its report;
    synthesise one so the orchestrator still has something to review."""
    lines = [
        f"# Delegated run {env.run_id} ({env.provider}) — {env.stop_reason}",
        "",
        "> Auto-generated stub: the run ended before the worker wrote its own "
        "report. Review the diff and the tail below.",
        "",
        f"- stopReason: {env.stop_reason}",
        f"- turns: {env.turns}",
        f"- cost: {env.cost_usd}",
        f"- diffStat: {env.diff_stat}",
        f"- diffPath: {env.diff_path}",
        "",
        "## Last assistant output",
        "",
    ]
    lines += acc.last_texts or ["(none captured)"]
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def _classify_stop(env: Envelope, acc: _Accum, provider: Provider, log_path: Path,
                   rc: int | None, cancelled: bool, timed_out: bool,
                   timeout_reason: tuple[str, float] | None) -> None:
    """How the worker ended, unless an earlier step already failed the run."""
    if env.error is not None:
        return
    if cancelled:
        env.stop_reason, env.exit_code = STOP_CANCELLED, 1
    elif acc.stop_reason in ("cancelled", "canceled", "aborted"):
        # Nobody asked delegate to cancel: the runner ended its own run
        # (grok reports `--max-turns` exhaustion as "cancelled"). That is
        # a worker failure with a reason, not an operator cancel.
        why = acc.error or _runner_error(log_path) or "no reason on stderr"
        env.fail(f"{provider.runner} ended the run itself ({acc.stop_reason}): {why}", 1,
                 acc.error_kind or "other")
    elif timed_out:
        why, duration = timeout_reason or ("idle", 0.0)
        tools, writes = acc.counts["tool_call"], acc.counts["write"]
        if why == "hard_cap":
            error = (f"hard cap {_fmt_duration(duration)} reached "
                     f"({tools} tool calls, {writes} writes)")
        else:
            error = (f"idle {_fmt_duration(duration)}: no runner output since "
                     f"{acc.last_kind or 'start'} "
                     f"({tools} tool calls, {writes} writes)")
        env.fail(error, 1, "timeout", stop=STOP_TIMEOUT)
    elif rc != 0:
        why = acc.error or _runner_error(log_path)
        env.fail(f"{provider.runner} exited {rc}" + (f": {why}" if why else ""), 1,
                 acc.error_kind or "other")
    elif acc.error:
        # The runner exited 0 but its stream ended on an error.
        env.fail(f"{provider.runner} exited 0 but reported an error: {acc.error}", 1,
                 acc.error_kind or "other")
    else:
        env.stop_reason, env.exit_code = STOP_END, 0


def _apply_contracts(job: Job, env: Envelope, meta0: dict | None) -> None:
    """The containment checks; each outranks the worker's own outcome."""
    # --read-only is a contract, not a hint: any delta fails the run, whatever
    # the worker's exit. Ignored files stay outside snapshots.
    if job.read_only and not env.empty_diff:
        env.fail(f"work tree changed while the read-only run was active "
                 f"({env.diff_stat}); the worker or another process wrote it; "
                 f"see {env.diff_path}", 2)
    # What git runs and sees is config, not content: a change is reported, never
    # repaired, and (like HEAD below) outranks the worker's own exit.
    touched = gitguard.changed(meta0, gitguard.fingerprint(job.cwd) or {}) if meta0 else []
    if touched:
        shown = ", ".join(touched[:5]) + (f" (+{len(touched) - 5} more)" if len(touched) > 5 else "")
        env.fail(f"worker changed git metadata ({shown}); the wrapper never repairs "
                 f"it. Inspect: git -C {job.cwd} config --show-origin --list", 2)
    # SAFE-1 has precedence over worker exit, cancellation, and timeout.
    head_after = git_head(job.cwd)
    if env.base_commit is not None and head_after != env.base_commit:
        env.fail(f"worker moved HEAD during the run ({env.base_commit} -> {head_after}); "
                 "the wrapper never commits or resets history; inspect the repository", 2)


def run(job: Job, provider: Provider, *, idle_timeout: float | None = None,
        hard_cap: float | None = None, heartbeat: Path | None = None,
        quiet: bool = False) -> Envelope:
    """One lifecycle for all providers; provider hooks cannot bypass containment."""
    if idle_timeout is None:
        idle_timeout = _env_seconds("DELEGATE_IDLE_TIMEOUT", IDLE_TIMEOUT)
    if hard_cap is None:
        hard_cap = _env_seconds("DELEGATE_HARD_CAP", HARD_CAP)
    started = time.monotonic()
    env = envelope_for(job, provider)
    job.run_dir.mkdir(parents=True, exist_ok=True)
    log_path, stream_path = job.run_dir / "runner.log", job.run_dir / "stream.jsonl"
    env.log_path, env.stream_path = str(log_path), str(stream_path)
    acc = _Accum()
    liveness = _Liveness()
    cancelled, finished = threading.Event(), threading.Event()
    timed_out = False
    timeout_reason: tuple[str, float] | None = None
    proc = None
    monitor = None
    tree0 = None
    meta0 = None
    rc = None
    previous_signals = {}

    def request_cancel(signum, frame):
        cancelled.set()

    def watch():
        nonlocal timed_out, timeout_reason
        log_size = 0
        while not finished.wait(0.1):
            if (job.run_dir / "cancel").exists():
                cancelled.set()
            if cancelled.is_set():
                group.kill()
                return
            # stderr is the other sign of life: runner.log grows.
            try:
                size = log_path.stat().st_size
            except OSError:
                size = log_size
            if size > log_size:
                log_size = size
                liveness.touch()
            now = time.monotonic()
            if now - started > hard_cap:
                timed_out, timeout_reason = True, ("hard_cap", now - started)
                group.kill()
                return
            silent = liveness.idle_for(now)
            if silent > idle_timeout:
                timed_out, timeout_reason = True, ("idle", silent)
                group.kill()
                return

    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous_signals[sig] = signal.signal(sig, request_cancel)
        env.base_commit, env.dirty_before = git_head(job.cwd), git_dirty(job.cwd)
        provider.prepare(job, env)
        meta0 = gitguard.fingerprint(job.cwd)
        tree0 = snapshot_tree(job.cwd, job.run_dir / "index0")
        if (job.run_dir / "cancel").exists():
            cancelled.set()
        if not cancelled.is_set():
            argv = provider.command(job)
            _record_command(job, argv)
            feed = job.run_dir / "prompt.txt" if provider.prompt_via == "stdin" else None
            proc = _spawn(argv, job.cwd, log_path, provider.environment(job), feed)
            group = _WorkerGroup(proc)
            rec = read_job(job.run_id)
            if rec is not None:
                rec.worker_pid = proc.pid
                rec.save()
            monitor = threading.Thread(target=watch, daemon=True)
            monitor.start()
            _stream(proc, provider, stream_path, heartbeat, acc, quiet,
                    job.run_dir, liveness)
            rc = proc.wait()
    except Exception as e:
        env.fail(f"run failed: {e}", 2)
    finally:
        finished.set()
        if monitor is not None:
            monitor.join()
        if proc is not None:
            # Unconditional: a leader that exited 0 can still have left
            # background children alive in the group (SAFE: no worker write
            # may outlive the snapshot below).
            group.kill()
            if proc.stdout is not None:
                proc.stdout.close()
        liveness.finish()
        _write_progress(job.run_dir, acc, liveness)
        try:
            # Shared authoritative post-run, including cancelled/failed workers.
            if tree0 is not None:
                measure_delta(job.cwd, job.run_dir, tree0, job.scope, job.report, env)
        except Exception as e:
            # Nothing here may escape the teardown: provider cleanup, the
            # envelope, and signal restoration all still have to happen.
            env.fail(f"post-run diff failed: {e}", 2)

        env.turns, env.cost_usd = acc.turns, acc.cost_usd
        env.failed_tool_calls = acc.failed_tool_calls
        if acc.usage["requests"]:
            env.provider_extra["usage"] = dict(acc.usage)
        _classify_stop(env, acc, provider, log_path, rc, cancelled.is_set(),
                       timed_out, timeout_reason)
        try:
            if rc is not None:
                provider.finish(job, env, rc)
        except Exception as e:
            if env.stop_reason == STOP_END:
                env.fail(f"provider result failed: {e}", 2)
        finally:
            try:
                provider.cleanup()
            except Exception as e:
                env.fail(f"provider cleanup failed: {e}", 2)
        # A wrong answer shape is a worker failure, like a non-zero runner exit.
        # A right one is saved as the bare object: answerPath then needs no parsing.
        # A failed write stops here, so the read-only and HEAD checks still run.
        try:
            answer = _record_answer(job, provider, env, acc)
            if job.schema is not None and env.stop_reason == STOP_END:
                obj, problem = check_answer(answer, job.schema)
                if problem:
                    env.fail(f"answer does not satisfy --schema: {problem}", 1)
                else:
                    path = job.run_dir / "answer.json"
                    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
                    env.answer_path = str(path)
        except OSError as e:
            if env.error is None:
                env.fail(f"answer capture failed: {e}", 2)
        _apply_contracts(job, env, meta0)
        if env.error_kind == "usage_limit" and acc.resets_at is not None:
            env.resets_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(acc.resets_at))
        if job.report is not None:
            if not job.report.exists() and env.stop_reason in (STOP_CANCELLED, STOP_TIMEOUT):
                _write_stub_report(job.report, env, acc)
            env.report_exists = job.report.exists()
        env.duration_seconds = int(time.monotonic() - started)
        env.provider_extra["maxIdleSeconds"] = int(liveness.max_idle)
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)
    return env
