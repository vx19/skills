"""Background job records and their lifecycle.

State lives under `state_root()/<run_id>/`:
  job.json     — record (provider, cwd, scope, pid, pgid, status, times)
  result.json  — the envelope, written once the run ends (terminal signal)
  live.lock    — flock held for the run; the kernel drops it when the wrapper dies
  heartbeat    — mtime bumped every streamed line; per-run progress display
  stream.jsonl, runner.log, diff.patch

Wrapper liveness cannot use ps/pgrep (the orchestrator may run sandboxed,
and a pid can be recycled by an unrelated process). So the authority is a kernel
object: the run's `live.lock` flock, held for the whole run and released when the
holder dies. `job.json`'s pid is the fallback for records written without a lock
(older records, non-POSIX hosts), and `heartbeat` is display only.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .lib.paths import state_root

_PRUNE_INTERVAL = 24 * 60 * 60
_PRUNE_STAMP = ".pruned"
_REASON_LIMIT = 300


@dataclass
class JobRecord:
    run_id: str
    provider: str
    cwd: str
    scope: list[str] = field(default_factory=list)
    status: str = "running"          # running | done | cancelled | error
    pid: int | None = None
    pgid: int | None = None
    worker_pid: int | None = None
    started_at: float = 0.0
    finished_at: float | None = None
    background: bool = False

    def path(self) -> Path:
        return state_root() / self.run_id / "job.json"

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(p)


def run_dir_for(run_id: str) -> Path:
    return state_root() / run_id


def read_job(run_id: str) -> JobRecord | None:
    p = state_root() / run_id / "job.json"
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return JobRecord(**{k: d[k] for k in d if k in JobRecord.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None


def read_result(run_id: str) -> dict[str, Any] | None:
    p = state_root() / run_id / "result.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_progress(run_id: str) -> dict[str, Any] | None:
    """Return `<run_id>/progress.json`, or None when it is missing or corrupt."""
    p = run_dir_for(run_id) / "progress.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_jobs() -> list[JobRecord]:
    root = state_root()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir(), reverse=True):
        if d.is_dir():
            r = read_job(d.name)
            if r is not None:
                out.append(r)
    return out


def live_jobs() -> list[JobRecord]:
    """Jobs genuinely still running: recorded running, no result.json, and the
    wrapper still alive. A dead wrapper with no result is a crash, not a job."""
    out = []
    for j in list_jobs():
        if j.status != "running" or read_result(j.run_id) is not None:
            continue
        if not is_wrapper_alive(j):
            continue  # crashed before writing result
        out.append(j)
    return out


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours to signal — sandbox may deny info
    except OSError:
        return False


LIVE_LOCK_NAME = "live.lock"


class RunIdCollision(RuntimeError):
    """A run id that is already active — never share a run dir."""


def _fcntl():
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        return None
    return fcntl


def hold_live_lock(run_id: str) -> int | None:
    """Take this run's liveness lock; keep the fd for the wrapper's lifetime.

    Called before the job record is saved, so a record on disk always means the
    lock was taken — a probe can never catch a wrapper that is merely slow to
    start. Returns None where flock does not exist (non-POSIX), which leaves the
    pid fallback in charge exactly as before.
    """
    fcntl = _fcntl()
    if fcntl is None:  # pragma: no cover - non-POSIX
        return None
    path = run_dir_for(run_id) / LIVE_LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RunIdCollision(f"run {run_id} is already active") from None
    return fd


def wrapper_alive(run_id: str) -> bool | None:
    """True/False from the run's liveness lock; None when there is no lock to ask.

    The probe takes the lock for an instant: if it succeeds, nobody held it, so
    the wrapper is gone — and a pid recycled by an unrelated process cannot claim
    otherwise. Closing the fd releases the probe's own lock.
    """
    fcntl = _fcntl()
    if fcntl is None:  # pragma: no cover - non-POSIX
        return None
    try:
        fd = os.open(str(run_dir_for(run_id) / LIVE_LOCK_NAME), os.O_RDWR)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        return False
    finally:
        os.close(fd)


def is_wrapper_alive(rec: JobRecord) -> bool:
    """Is this run's wrapper still around?

    Prefers the liveness lock: a pid can be recycled by an unrelated process,
    which would otherwise pin a crashed run's state in place forever. Records
    written without a lock fall back to the pid, exactly as before.
    """
    state = wrapper_alive(rec.run_id)
    return state if state is not None else bool(rec.pid) and pid_alive(rec.pid)


def heartbeat_age(run_id: str, now: float | None = None) -> float | None:
    """Seconds since the heartbeat file was touched. `now` defaults to time.time()."""
    hb = state_root() / run_id / "heartbeat"
    try:
        if not hb.is_file():
            return None
        stamp = time.time() if now is None else now
        return stamp - hb.stat().st_mtime
    except OSError:
        return None


def _as_int(val: Any, default: int = 0) -> int:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return default
    return int(val)


def _fmt_duration(seconds: float) -> str:
    """Compact age: `45s`, `3m05s`, `1h02m`."""
    whole = int(seconds)
    if whole < 0:
        whole = 0
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        minutes, secs = divmod(whole, 60)
        return f"{minutes}m{secs:02d}s"
    hours, rem = divmod(whole, 3600)
    return f"{hours}h{rem // 60:02d}m"


def _ago(at: Any, now: float) -> str:
    try:
        stamp = float(at)
    except (TypeError, ValueError):
        return "never"
    return f"{_fmt_duration(now - stamp)} ago"


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def format_live_status(rec: JobRecord, progress: dict | None, now: float) -> str:
    """One line for a live job. Missing progress is treated as empty."""
    if not isinstance(progress, dict):
        progress = {}
    kind = progress.get("lastKind")
    if not isinstance(kind, str) or not kind:
        kind = "event"
    counts = progress.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    parts = [
        f"running {_fmt_duration(now - rec.started_at)}",
        f"last {kind} {_ago(progress.get('lastEventAt'), now)}",
        f"last tool {_ago(progress.get('lastToolAt'), now)}",
        f"last write {_ago(progress.get('lastWriteAt'), now)}",
        f"tools {_as_int(counts.get('tool_call'))} "
        f"(writes {_as_int(counts.get('write'))}, failed {_as_int(counts.get('failed_tool'))})",
    ]
    usage = progress.get("usage")
    if isinstance(usage, dict) and _as_int(usage.get("requests")) > 0:
        parts.append(
            f"tokens out {_fmt_tokens(_as_int(usage.get('output')))} "
            f"(reasoning {_fmt_tokens(_as_int(usage.get('reasoning')))})"
        )
    hb = heartbeat_age(rec.run_id, now)
    parts.append("heartbeat never" if hb is None else f"heartbeat {_fmt_duration(hb)} ago")
    return " \u00b7 ".join(parts)


def scope_overlap(scope: list[str]) -> list[str]:
    """Return run_ids of live jobs whose scope intersects `scope` (advisory)."""
    if not scope:
        return []
    want = {s.rstrip("/") for s in scope}
    hits = []
    for j in live_jobs():
        other = {s.rstrip("/") for s in j.scope}
        if want & other or (not other):  # empty scope = whole tree = always overlaps
            hits.append(j.run_id)
    return hits


def cancel(run_id: str) -> tuple[bool, str]:
    rec = read_job(run_id)
    if rec is None:
        return False, f"no such job: {run_id}"
    if read_result(run_id) is not None:
        return False, f"job {run_id} already finished"
    if not is_wrapper_alive(rec):
        return False, f"job {run_id} wrapper is gone; inspect the recorded worker before repair"
    # A durable request also covers the background fork/startup race. Core's
    # monitor kills its own child group and then writes a cancelled envelope.
    (run_dir_for(run_id) / "cancel").touch()
    return True, f"cancellation requested for job {run_id}"


def _worker_group_gone(rec: JobRecord) -> bool:
    """Whether the worker's process group can no longer be signalled.

    A record written before `worker_pid` existed cannot prove a live group, and
    its wrapper is already gone, so it converges rather than staying stalled.
    """
    pid = rec.worker_pid or rec.pgid
    if not pid:
        return True
    try:
        group = os.getpgid(pid)
        os.killpg(group, 0)
    except ProcessLookupError:
        return not pid_alive(pid)
    except OSError:
        return False  # exists, not ours to signal — never settle it dead
    return False


def _stream_error(run_id: str, provider_name: str) -> str | None:
    """Last parsed runner error in the stream, at most 300 characters."""
    try:
        from .providers import get

        prov = get(provider_name)
        if prov is None or not prov.parses_stream:
            return None
        stream = run_dir_for(run_id) / "stream.jsonl"
        if not stream.is_file():
            return None
        reason = None
        with stream.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                ev = prov.parse(line)
                if ev is not None and ev.kind == "error" and ev.text:
                    reason = ev.text[:_REASON_LIMIT]
        return reason
    except Exception:
        return None


def settle_dead(rec: JobRecord) -> dict[str, Any] | None:
    """Write one error envelope for a run whose wrapper died without a result.

    Only a dead wrapper and a dead worker group qualify. The write is atomic and
    skipped when `result.json` already exists, so every later call is a no-op.
    """
    if (run_dir_for(rec.run_id) / "result.json").is_file():
        return None
    if is_wrapper_alive(rec):
        return None
    if not _worker_group_gone(rec):
        return None

    from .envelope import Envelope, STOP_ERROR

    error = "wrapper died before writing a result"
    why = _stream_error(rec.run_id, rec.provider)
    if why:
        error += f": {why}"
    env = Envelope(provider=rec.provider, run_id=rec.run_id, cwd=rec.cwd)
    env.stop_reason, env.exit_code, env.error = STOP_ERROR, 1, error
    payload = env.to_json()

    dest = run_dir_for(rec.run_id) / "result.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".result.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return payload


def _keep_days() -> float | None:
    """Days to keep finished runs; None (no pruning) unless DELEGATE_KEEP_DAYS is set."""
    try:
        days = float(os.environ.get("DELEGATE_KEEP_DAYS", ""))
    except ValueError:
        return None
    return days if days >= 0 else None


def prune_runs(*, keep_days: float | None = None,
               now: float | None = None) -> list[str]:
    """Delete old finished run dirs, at most once per 24 hours. Opt-in.

    Only a directory with a `result.json`, no live wrapper, and an mtime older
    than the window is removed. The stamp lives in the state root, so a crash
    mid-scan costs one day of deferral rather than a scan on every run.
    """
    days = _keep_days() if keep_days is None else keep_days
    if days is None:
        return []
    root = state_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []
    if not root.is_dir():
        return []
    stamp = root / _PRUNE_STAMP
    when = time.time() if now is None else now
    try:
        if stamp.is_file() and when - stamp.stat().st_mtime < _PRUNE_INTERVAL:
            return []
    except OSError:
        pass

    cutoff = when - max(days, 0) * 86400
    removed = []
    for d in sorted(root.iterdir()):
        try:
            if not d.is_dir() or d.is_symlink():
                continue
            result = d / "result.json"
            if not result.is_file():
                continue
            age_stamp = min(d.stat().st_mtime, result.stat().st_mtime)
            if age_stamp >= cutoff:
                continue
            rec = read_job(d.name)
            if rec is not None and is_wrapper_alive(rec):
                continue
            shutil.rmtree(d)
            removed.append(d.name)
        except OSError:
            continue
    try:
        stamp.write_text("", encoding="utf-8")
        os.utime(stamp, (when, when))
    except OSError:
        pass
    return removed


def daemonize() -> bool:
    """Double-fork into a detached background process.

    Returns True in the surviving daemon child, False in the original parent
    (which should print the job id and exit). The daemon's stdio is detached.
    """
    if os.fork() > 0:
        return False              # original parent
    os.setsid()
    if os.fork() > 0:
        os._exit(0)               # first child exits, orphaning the grandchild
    # grandchild = daemon
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    return True
