"""Worker-delta snapshots via a private git index.

Never writes the repository's real `.git/index`. New files appear because
`git add -A` runs against GIT_INDEX_FILE only.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


class SnapshotError(RuntimeError):
    """Private-index `git add -A` failed; wrapper must not treat this as success."""


def snapshot_tree(cwd: Path, index_file: Path) -> str:
    """Read HEAD + worktree into a private index and return the write-tree id."""
    env = _index_env(index_file)
    _git(cwd, env, "read-tree", "HEAD", check=True)
    _add_all(cwd, env)
    proc = _git(cwd, env, "write-tree", check=True)
    return (proc.stdout or "").strip()


def diff_from_tree(cwd: Path, tree_id: str, index_file: Path) -> str:
    """Private-index worktree snapshot minus ``tree_id`` (`git diff --cached`)."""
    env = _index_env(index_file)
    _git(cwd, env, "read-tree", "HEAD", check=True)
    _add_all(cwd, env)
    proc = _git(
        cwd,
        env,
        "--no-pager",
        "diff",
        "--cached",
        "--no-color",
        tree_id,
        check=True,
    )
    return proc.stdout or ""


def diff_paths(diff_text: str) -> list[str]:
    seen: list[str] = []
    for line in diff_text.splitlines():
        parsed = _split_git_paths(line)
        if parsed is None:
            continue
        for part in parsed:
            if part in ("/dev/null", ""):
                continue
            if part not in seen:
                seen.append(part)
    return seen


def _split_git_paths(line: str) -> tuple[str, str] | None:
    """Parse a `diff --git` header as this git emits it.

    Git 2.54 (and current 2.x): spaces are *not* quoted
    (`a/weird file.txt b/weird file.txt`). Tabs/non-ASCII are C-quoted
    (`"a/tab\\tname.txt"`, `"a/caf\\303\\251.txt"`).
    """
    prefix = "diff --git "
    if not line.startswith(prefix):
        return None
    rest = line[len(prefix) :]
    if rest.startswith('"'):
        tokens = _tokenize_cquoted(rest)
        if len(tokens) != 2:
            return None
        return _strip_ab(tokens[0]), _strip_ab(tokens[1])
    # Unquoted: split on " b/" after an "a/" prefix (paths may contain spaces).
    if rest.startswith("a/") and " b/" in rest:
        left, right = rest.split(" b/", 1)
        return _strip_ab(left), _strip_ab("b/" + right)
    return None


def _tokenize_cquoted(rest: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    n = len(rest)
    while i < n:
        while i < n and rest[i] == " ":
            i += 1
        if i >= n:
            break
        if rest[i] != '"':
            return []
        i += 1
        raw: list[str] = []
        while i < n and rest[i] != '"':
            if rest[i] == "\\" and i + 1 < n:
                raw.append("\\")
                raw.append(rest[i + 1])
                i += 2
                continue
            raw.append(rest[i])
            i += 1
        if i >= n or rest[i] != '"':
            return []
        i += 1
        tokens.append(_decode_git_c_quote("".join(raw)))
    return tokens


_C_SIMPLE = {
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "\\": "\\",
    '"': '"',
}


def _decode_git_c_quote(body: str) -> str:
    """Decode git quote.c escapes (simple + 1–3 digit octal)."""
    out = bytearray()
    i = 0
    while i < len(body):
        if body[i] != "\\" or i + 1 >= len(body):
            out.extend(body[i].encode("utf-8", errors="surrogateescape"))
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in _C_SIMPLE:
            out.extend(_C_SIMPLE[nxt].encode("latin-1"))
            i += 2
            continue
        if nxt in "01234567":
            j = i + 1
            digits = []
            while j < len(body) and body[j] in "01234567" and len(digits) < 3:
                digits.append(body[j])
                j += 1
            out.append(int("".join(digits), 8))
            i = j
            continue
        out.extend(body[i].encode("utf-8", errors="surrogateescape"))
        i += 1
    return out.decode("utf-8", errors="surrogateescape")


def _strip_ab(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


# Seatbelt (and some permission policies) answer `stat` with EPERM. Git then
# aborts `add -A` on the first such path, which makes every snapshot fail when
# delegate itself is running inside grok. Those paths are already invisible to
# the worker, so they are left out of the private index and named on stderr.
_UNREADABLE = re.compile(
    r"unable to stat '([^']+)': (?:Operation not permitted|Permission denied)"
)


def _add_all(cwd: Path, env: dict[str, str]) -> None:
    excludes: list[str] = []
    while True:
        args = ["add", "-A"]
        if excludes:
            args += ["--", ".", *(f":(exclude){path}" for path in excludes)]
        proc = _git(cwd, env, *args, check=False)
        if proc.returncode == 0:
            if excludes:
                print(
                    "delegate: warning: snapshot left out paths the sandbox "
                    "will not stat: " + ", ".join(excludes),
                    file=sys.stderr,
                )
            return
        err = (proc.stderr or proc.stdout or "").strip() or "unknown git error"
        fresh = [path for path in _UNREADABLE.findall(err) if path not in excludes]
        if not fresh or len(excludes) + len(fresh) > 64:
            raise SnapshotError(f"git add -A failed (exit {proc.returncode}): {err}")
        excludes.extend(fresh)
        # A failed add can leave the private index half-updated. Rebuild it
        # before the next attempt so the exclude list is the only difference.
        _git(cwd, env, "read-tree", "HEAD", check=True)


def _index_env(index_file: Path) -> dict[str, str]:
    index_file = Path(index_file)
    index_file.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index_file.resolve())
    return env


def _git(
    cwd: Path, env: dict[str, str], *args: str, check: bool
) -> subprocess.CompletedProcess[str]:
    """Run git, decoding output with surrogateescape, or raise SnapshotError.

    Git copies file bytes verbatim into a diff body, so a worktree holding a
    latin-1 (or otherwise non-UTF-8) file yields bytes that are not valid UTF-8.
    Surrogates round-trip those bytes back unchanged; a real git failure still
    raises. This never lets a UnicodeDecodeError escape mid-teardown.
    """
    raw = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=env,
        check=False,
        capture_output=True,
    )
    proc: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
        raw.args,
        raw.returncode,
        raw.stdout.decode("utf-8", errors="surrogateescape"),
        raw.stderr.decode("utf-8", errors="surrogateescape"),
    )
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or "unknown git error"
        raise SnapshotError(f"git {' '.join(args)} failed (exit {proc.returncode}): {err}")
    return proc
