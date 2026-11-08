"""Shared test fixtures: a scratch git repository and an in-process CLI call."""

from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    """Run git in `repo`; return stdout, stripped. A failure raises."""
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def init_repo(repo: Path, files: dict[str, str] | None = None) -> Path:
    """A repository at `repo` with one commit of `files` (default: `base`).

    A local user is configured, so a test can commit without `-c` flags.
    """
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@test")
    for name, text in (files or {"base": "base\n"}).items():
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    return repo


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run `delegate` in-process; return (exit code, stdout, stderr)."""
    from delegate import cli

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()
