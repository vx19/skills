"""Platform paths and git helpers. No PATH mutation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .constants import STATE_DIRNAME


def state_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local"
        )
        return Path(base) / STATE_DIRNAME
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / STATE_DIRNAME
    return Path.home() / ".local" / "state" / STATE_DIRNAME


def host_label() -> str:
    plat = sys.platform
    if plat == "darwin":
        return "macOS"
    if plat.startswith("win"):
        return "Windows"
    if plat.startswith("linux"):
        return "Linux"
    return plat


def git_ok(cwd: Path) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
        ).returncode
        == 0
    )


def git_head(cwd: Path) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def git_toplevel(cwd: Path) -> Path | None:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    return Path(raw).resolve() if raw else None


def git_common_dir(cwd: Path) -> Path | None:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = cwd / p
    return p.resolve()


def git_dirty(cwd: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    return bool((proc.stdout or "").strip())
