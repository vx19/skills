"""Keep a repository's own config from running commands in the wrapper's git.

The wrapper runs git in the target repo outside any provider sandbox, so a
worker that can write `.git/config` could make that git run its code. `hardened`
neutralizes the config keys that execute a command. `fingerprint` lets the run
notice when the worker touched the files that decide what git does or sees.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

_BASE = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")


def hardened(cwd: Path) -> list[str]:
    """`git` options that stop config-driven commands, for one invocation.

    Each configured filter is rebuilt as `cat`: a worker-defined clean, smudge
    or process command never starts, and a filter marked `required` falls back
    to the raw file instead of failing the snapshot.
    """
    args = list(_BASE)
    proc = subprocess.run(
        ["git", "-C", str(cwd), "config", "-z", "--get-regexp", r"^filter\."],
        capture_output=True,
    )
    names: set[str] = set()
    for entry in proc.stdout.decode("utf-8", errors="replace").split("\0"):
        key = entry.split("\n", 1)[0]
        if key.startswith("filter.") and key.count(".") >= 2:
            names.add(key[len("filter."):].rsplit(".", 1)[0])
    for name in sorted(names):
        args += ["-c", f"filter.{name}.required=false",
                 "-c", f"filter.{name}.process=false",
                 "-c", f"filter.{name}.clean=cat",
                 "-c", f"filter.{name}.smudge=cat"]
    return args


def _git_out(cwd: Path, *args: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="surrogateescape")


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "-"


def fingerprint(cwd: Path) -> dict[str, str] | None:
    """Digest of git's behaviour-setting state, or None outside a repository.

    Covers config, hooks, attributes and exclude files, the checked-out branch
    name and local branch and tag tips. Remote-tracking refs are left out so a
    background `git fetch` is not mistaken for the worker.
    """
    dirs = _git_out(cwd, "rev-parse", "--git-dir", "--git-common-dir")
    if dirs is None:
        return None
    git_dir, common_dir = (Path(cwd, line).resolve() for line in dirs.splitlines()[:2])
    state: dict[str, str] = {}
    for base in dict.fromkeys((git_dir, common_dir)):
        for rel in ("config", "config.worktree", "info/attributes", "info/exclude"):
            state[f".git/{rel}" if base == common_dir else f".git/{rel} (worktree)"] = \
                _digest(base / rel)
    hooks = common_dir / "hooks"
    if hooks.is_dir():
        for hook in sorted(p for p in hooks.rglob("*") if p.is_file()):
            state[f".git/hooks/{hook.relative_to(hooks)}"] = _digest(hook) + (
                "x" if hook.stat().st_mode & 0o111 else "")
    state["HEAD branch"] = (_git_out(cwd, "symbolic-ref", "-q", "HEAD") or "").strip()
    refs = _git_out(cwd, "for-each-ref", "--format=%(refname) %(objectname)",
                    "refs/heads", "refs/tags") or ""
    for line in refs.splitlines():
        name, _, sha = line.rpartition(" ")
        state[name] = sha
    return state


def changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Names whose digest differs, appeared or vanished between two fingerprints."""
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
