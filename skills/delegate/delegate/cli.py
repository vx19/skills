"""`delegate` command-line surface.

Subcommands: run | status | wait | result | logs | cancel | doctor.

The command string the user types must be stable
and match the sandbox allow/exempt patterns, so every input is a flag (no env
vars), and the CLI owns logging + backgrounding (no pipes, `&`, or nohup in the
typed command). Bypass-class flags never appear here — they live in providers/.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from . import jobs as jobsmod
from .answer import SchemaError, instruction, read_schema
from .envelope import Envelope
from .lib.paths import git_head, git_ok, host_label
from .prompt import PromptError, assemble, read_rules, read_task
from .providers import NAMES, get
from .providers.base import Job

USAGE_EXIT = 64
WAIT_PENDING_EXIT = 3  # wait --timeout: the job is still live


def _die(code: int, msg: str) -> "int":
    print(f"delegate: {msg}", file=sys.stderr)
    return code


def _new_run_id() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(2)


# --------------------------------------------------------------------------- run
def _build_job(args) -> Job:
    provider = args.provider
    cwd = Path(args.cwd).resolve()

    # task: --task FILE, or positional prompt.
    positional = " ".join(args.prompt).strip() if args.prompt else ""
    if args.task and positional:
        raise PromptError("give either --task FILE or a prompt argument, not both")
    if args.task:
        task = read_task(args.task, task_is_file=True)
    else:
        task = read_task(positional or None, task_is_file=False)
    rules = read_rules(args.rules or [])
    report = Path(args.report).resolve() if args.report else None
    read_only = bool(args.read_only)
    prov = get(provider)
    if read_only:
        if prov.read_only is None:
            raise PromptError(f"--read-only: {provider} has no mode that stops writes; "
                              "use a provider that does (grok, claude, codex, cursor)")
        if args.no_sandbox:
            raise PromptError("--read-only and --no-sandbox contradict each other")
        if report is not None:
            raise PromptError("--read-only cannot ask the worker to write --report; "
                              "its final message is captured to answerPath instead")
    schema = None
    if args.schema:
        if not prov.answers:
            raise PromptError(f"--schema: {provider} has no capturable final answer")
        try:
            schema = read_schema(args.schema)
        except SchemaError as e:
            raise PromptError(str(e)) from None
        task += "\n\n" + instruction(schema)
    computer_use = bool(args.computer_use)
    if computer_use and not prov.computer_use:
        raise PromptError(f"--computer-use: {provider} has no desktop-control tool; "
                          "use codex")
    if report is not None:
        task += f"\n\nWrite your report to {report} before final verification."
    prompt = assemble(task, rules, read_only=read_only, computer_use=computer_use)

    extra: dict[str, str] = {}
    for kv in args.provider_arg or []:
        if "=" not in kv:
            raise PromptError(f"--provider-arg must be KEY=VALUE (got {kv!r})")
        k, v = kv.split("=", 1)
        if k not in prov.extra_keys:
            known = ", ".join(sorted(prov.extra_keys)) or "none"
            raise PromptError(f"--provider-arg {k!r} is not read by {provider} "
                              f"(keys: {known})")
        extra[k] = v

    run_id = _new_run_id()
    job = Job(
        provider=provider,
        cwd=cwd,
        prompt=prompt,
        run_id=run_id,
        run_dir=jobsmod.run_dir_for(run_id),
        model=args.model,
        effort=args.effort,
        max_turns=args.max_turns,
        scope=list(args.scope or []),
        report=report,
        extra=extra,
        no_sandbox=bool(args.no_sandbox),
        dry_run=bool(args.dry_run),
        read_only=read_only,
        web=bool(args.web),
        schema=schema,
        computer_use=computer_use,
    )
    err = prov.validate(job)
    if err:
        raise PromptError(err)
    return job


def cmd_run(args) -> int:
    from . import core

    prov = get(args.provider)
    try:
        job = _build_job(args)
    except PromptError as e:
        return _die(USAGE_EXIT, str(e))
    for flag, value, supported in (
        ("--effort", job.effort, ("grok", "claude", "codex", "pi")),
        ("--max-turns", job.max_turns, ("grok", "claude")),
    ):
        if value is not None and prov.name not in supported:
            print(f"delegate: warning: {flag} is not supported by {prov.name}; ignored",
                  file=sys.stderr)
    if job.web and not job.read_only:
        print("delegate: warning: --web only matters with --read-only; ignored",
              file=sys.stderr)
    elif job.read_only and not prov.web_toggle:
        print(f"delegate: warning: delegate cannot switch web access for {prov.name}; "
              "its own default applies", file=sys.stderr)

    # Advisory overlap warning; it never blocks.
    overlaps = jobsmod.scope_overlap(job.scope)
    if overlaps:
        print(f"delegate: warning: live job(s) {', '.join(overlaps)} may touch the "
              f"same paths as --scope {job.scope}", file=sys.stderr)

    if getattr(prov, "unsandboxed", False):
        print(f"delegate: warning: {prov.name} runs unsandboxed with your full user "
              "privileges (no filesystem isolation).", file=sys.stderr)
    if prov.name == "grok":
        from .providers.grok import read_only_inherit_warning
        note = read_only_inherit_warning(job)
        if note:
            print(f"delegate: warning: {note}", file=sys.stderr)
    # Preflight (skip the heavy checks on dry-run so it stays offline).
    if not job.dry_run:
        jobsmod.prune_runs()
        try:
            core.preflight(job, prov)
        except core.Preflight as e:
            env = Envelope(provider=prov.name, run_id=job.run_id, cwd=str(job.cwd))
            env.read_only = job.read_only
            env.stop_reason = "error"
            env.exit_code = 2
            env.error = str(e)
            env.emit()
            return 2

    if job.dry_run:
        try:
            argv = prov.command(job)
        except ValueError as e:
            return _die(USAGE_EXIT, str(e))
        env = Envelope(provider=prov.name, run_id=job.run_id, cwd=str(job.cwd))
        env.dry_run = True
        env.read_only = job.read_only
        env.stop_reason = "end_turn"
        env.model = job.model
        env.provider_extra = {"argv": argv}
        env.emit()
        return 0

    try:
        if args.background:
            return _run_background(job, prov)
        env = _run_foreground(job, prov, quiet=args.quiet)
    except jobsmod.RunIdCollision as e:
        return _die(2, str(e))
    env.emit()
    return env.exit_code


def _run_foreground(job: Job, prov, *, quiet: bool) -> Envelope:
    from . import core

    rec = jobsmod.JobRecord(
        run_id=job.run_id, provider=prov.name, cwd=str(job.cwd),
        scope=list(job.scope), status="running",
        pid=os.getpid(), pgid=os.getpgid(0), started_at=time.time(),
        background=False,
    )
    jobsmod.hold_live_lock(job.run_id)  # before the record is visible; fd stays open
    rec.save()

    env = core.run(job, prov, heartbeat=job.run_dir / "heartbeat", quiet=quiet)

    result_tmp = job.run_dir / "result.tmp"
    result_tmp.write_text(json.dumps(env.to_json(), indent=2), encoding="utf-8")
    result_tmp.replace(job.run_dir / "result.json")
    rec = jobsmod.read_job(job.run_id) or rec
    rec.status = {0: "done"}.get(env.exit_code, env.stop_reason or "error")
    rec.finished_at = time.time()
    rec.save()
    return env


def _run_background(job: Job, prov) -> int:
    job.run_dir.mkdir(parents=True, exist_ok=True)
    # Record before forking so `status` sees it immediately.
    rec = jobsmod.JobRecord(
        run_id=job.run_id, provider=prov.name, cwd=str(job.cwd),
        scope=list(job.scope), status="running", started_at=time.time(),
        background=True,
    )
    # Before the record is visible; the daemon inherits the fd across the double
    # fork, so the lock outlives the parent that is about to return and exit.
    jobsmod.hold_live_lock(job.run_id)
    rec.save()
    if not jobsmod.daemonize():
        # original parent: hand back the handle and exit
        print(json.dumps({"jobId": job.run_id, "runDir": str(job.run_dir)}))
        return 0
    # daemon
    from . import core
    rec.pid = os.getpid()
    rec.pgid = os.getpgid(0)
    rec.save()
    try:
        env = core.run(
            job, prov, heartbeat=job.run_dir / "heartbeat", quiet=True)
    except Exception as e:  # never leave a job without a result
        env = Envelope(provider=prov.name, run_id=job.run_id, cwd=str(job.cwd))
        env.stop_reason = "error"
        env.exit_code = 2
        env.error = f"daemon crashed: {e}"
    result_tmp = job.run_dir / "result.tmp"
    result_tmp.write_text(json.dumps(env.to_json(), indent=2), encoding="utf-8")
    result_tmp.replace(job.run_dir / "result.json")
    rec = jobsmod.read_job(job.run_id) or rec
    rec.status = {0: "done"}.get(env.exit_code, env.stop_reason or "error")
    rec.finished_at = time.time()
    rec.save()
    os._exit(0)


# ------------------------------------------------------------------ status/wait
def cmd_status(args) -> int:
    if args.job:
        rec = jobsmod.read_job(args.job)
        if rec is None:
            return _die(1, f"no such job: {args.job}")
        jobsmod.settle_dead(rec)
        _print_status_row(rec, detail=True)
        return 0
    rows = jobsmod.list_jobs()
    if not rows:
        print("no delegate jobs recorded")
        return 0
    for rec in rows[: args.limit]:
        jobsmod.settle_dead(rec)
        _print_status_row(rec, detail=False)
    return 0


def _print_status_row(rec, *, detail: bool) -> None:
    result = jobsmod.read_result(rec.run_id)
    if result is not None:
        state = result.get("stopReason") or rec.status
    elif jobsmod.is_wrapper_alive(rec):
        state = jobsmod.format_live_status(
            rec, jobsmod.read_progress(rec.run_id), time.time())
    else:
        state = "stalled (no result, process gone)"
    line = f"{rec.run_id}  {rec.provider:6}  {state}"
    print(line)
    if detail and result is not None:
        for k in ("stopReason", "exitCode", "turns", "costUsd", "diffStat",
                  "reportExists", "diffPath", "error"):
            if result.get(k) is not None:
                print(f"    {k}: {result[k]}")
        _print_usage(result)


def _print_usage(result: dict) -> None:
    extra = result.get("providerExtra")
    if not isinstance(extra, dict):
        return
    usage = extra.get("usage")
    if not usage:
        return
    print(f"    usage: {json.dumps(usage)}")


def cmd_wait(args) -> int:
    deadline = time.time() + max(args.timeout, 0)
    run_id = args.job
    while True:
        rec = jobsmod.read_job(run_id)
        if rec is not None:
            jobsmod.settle_dead(rec)
        result = jobsmod.read_result(run_id)
        if result is not None:
            print(json.dumps(result))
            return int(result.get("exitCode", 0))
        if rec is None:
            return _die(1, f"no such job: {run_id}")
        live = jobsmod.is_wrapper_alive(rec)
        if not live:
            # The wrapper is gone but the worker group may still be finishing:
            # let settle_dead look once more after a short grace.
            time.sleep(0.5)
            result = jobsmod.settle_dead(rec) or jobsmod.read_result(run_id)
            if result is not None:
                print(json.dumps(result))
                return int(result.get("exitCode", 0))
            return _die(1, f"job {run_id} process gone with no result")
        if time.time() >= deadline:
            line = jobsmod.format_live_status(
                rec, jobsmod.read_progress(run_id), time.time())
            return _die(WAIT_PENDING_EXIT, f"still running: {line}")
        time.sleep(1.0)


def cmd_result(args) -> int:
    rec = jobsmod.read_job(args.job)
    if rec is not None:
        jobsmod.settle_dead(rec)
    result = jobsmod.read_result(args.job)
    if result is None:
        return _die(1, f"no result for job {args.job} (still running or unknown)")
    print(json.dumps(result))
    return int(result.get("exitCode", 0))


def _logs_parser(run_id: str):
    """The job's provider when it parses a stream; else None (raw fallback)."""
    rec = jobsmod.read_job(run_id)
    if rec is None:
        return None
    prov = get(rec.provider)
    if prov is None or not prov.parses_stream:
        return None
    return prov


def _compact_logs(provider, lines: list[str]) -> list[str]:
    """Tool calls, failed tool results, ends, and joined text. Thoughts collapse."""
    out: list[str] = []
    thoughts = 0
    text: list[str] = []

    def flush_thoughts() -> None:
        nonlocal thoughts
        if thoughts:
            out.append(f"  … thinking ({thoughts} events)")
            thoughts = 0

    def flush_text() -> None:
        if text:
            out.extend("".join(text).splitlines())
            text.clear()

    for line in lines:
        ev = provider.parse(line)
        if ev is None:
            continue
        if ev.kind == "thought":
            flush_text()
            thoughts += 1
            continue
        if ev.kind == "text":
            flush_thoughts()
            if ev.text:
                text.append(ev.text)
            continue
        flush_text()
        flush_thoughts()
        msg = ev.compact()
        if msg:
            out.append(msg)
    flush_text()
    flush_thoughts()
    return out


def cmd_logs(args) -> int:
    d = jobsmod.run_dir_for(args.job)
    stream = d / "stream.jsonl"
    if not stream.is_file():
        return _die(1, f"no stream log for job {args.job}")
    lines = stream.read_text(encoding="utf-8", errors="replace").splitlines()
    provider = None if args.raw else _logs_parser(args.job)
    shown = lines if provider is None else _compact_logs(provider, lines)
    for ln in shown[-args.tail:]:
        print(ln)
    return 0


def cmd_cancel(args) -> int:
    ok, msg = jobsmod.cancel(args.job)
    print(msg, file=sys.stderr)
    return 0 if ok else 1


# ---------------------------------------------------------------------- doctor
def cmd_doctor(args) -> int:
    names = [args.provider] if args.provider else list(NAMES)
    cwd = Path(args.cwd).resolve() if args.cwd else None
    fails = 0
    print(f"delegate {__version__} on {host_label()}")
    for name in names:
        prov = get(name)
        if prov is None:
            print(f"FAIL {name}: unknown provider"); fails += 1; continue
        path = shutil.which(prov.runner)
        if path is None:
            print(f"warn {name:6}: {prov.runner} not on PATH")
            continue
        print(f"ok   {name:6}: {prov.runner} -> {path}")
        if name == "grok":
            from .providers.grok import describe_sandbox
            print(f"ok   {name:6}: sandbox {describe_sandbox()}")
            cred = prov.preflight_check(
                Job(name, cwd or Path("."), "", "doctor", cwd or Path(".")))
            if cred:
                print(f"FAIL {name:6}: {cred}"); fails += 1
            else:
                print(f"ok   {name:6}: credentials readable")
        if prov.computer_use:
            ready, detail = prov.computer_use_status()
            # Optional capability: a missing plugin only matters for --computer-use.
            print(f"{'ok  ' if ready else 'warn'} {name:6}: computer use: {detail}")
        if cwd is not None:
            verdict = prov.trust(cwd)
            if verdict is not None and verdict[0] is False:
                print(f"FAIL {name:6}: {verdict[1]}"); fails += 1
            elif verdict is not None:
                print(f"ok   {name:6}: {cwd} trusted")
    if cwd is not None:
        if not git_ok(cwd):
            print(f"FAIL cwd: {cwd} is not a git work tree"); fails += 1
        elif git_head(cwd) is None:
            print(f"FAIL cwd: {cwd} has no commit"); fails += 1
        else:
            print(f"ok   cwd: {cwd} (git worktree)")
    return 2 if fails else 0


# ------------------------------------------------------------------------ link
# ------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="delegate", description="Unified multi-provider delegate CLI.")
    p.add_argument("-V", "--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one delegation")
    r.add_argument("--provider", required=True, choices=NAMES)
    r.add_argument("--cwd", default=".")
    r.add_argument("--model")
    r.add_argument("--effort")
    r.add_argument("--rules", action="append")
    r.add_argument("--task")
    r.add_argument("--max-turns", type=int)
    r.add_argument("--scope", action="append")
    r.add_argument("--report")
    r.add_argument("--provider-arg", action="append")
    r.add_argument("--background", action="store_true")
    r.add_argument("--no-sandbox", action="store_true")
    r.add_argument("--read-only", action="store_true")
    r.add_argument("--web", action="store_true")
    r.add_argument("--schema")
    r.add_argument("--computer-use", action="store_true",
                   help="let the worker drive desktop apps (codex Computer Use)")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--quiet", action="store_true")
    r.add_argument("prompt", nargs="*")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="show job status")
    s.add_argument("job", nargs="?")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_status)

    w = sub.add_parser(
        "wait",
        help="block until a job finishes (exit 3 if still running when --timeout ends)",
        description="Block until a job finishes. Exit 3 if it is still running "
                    "when --timeout ends. Unknown jobs and a dead wrapper with "
                    "no result still exit 1.",
    )
    w.add_argument("job")
    w.add_argument("--timeout", type=int, default=300,
                   help="seconds to wait (default 300); exit 3 if the job is still live")
    w.set_defaults(func=cmd_wait)

    re_ = sub.add_parser("result", help="print a finished job's envelope")
    re_.add_argument("job")
    re_.set_defaults(func=cmd_result)

    lg = sub.add_parser("logs", help="print a compact view of a job's stream")
    lg.add_argument("job")
    lg.add_argument("--tail", type=int, default=40)
    lg.add_argument("--raw", action="store_true",
                    help="print the raw JSONL tail; also used when the provider has no parser")
    lg.set_defaults(func=cmd_logs)

    c = sub.add_parser("cancel", help="cancel a running job")
    c.add_argument("job")
    c.set_defaults(func=cmd_cancel)

    d = sub.add_parser("doctor", help="check runners, git, trust")
    d.add_argument("--provider", choices=NAMES)
    d.add_argument("--cwd")
    d.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except PromptError as e:
        return _die(USAGE_EXIT, str(e))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
