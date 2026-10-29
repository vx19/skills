#!/usr/bin/env python3
"""Check a fresh publication clone with a scrubbed environment."""

import argparse
from contextlib import redirect_stderr
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


def _fingerprint(root: Path) -> tuple[str, int]:
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise OSError("watch root is not a directory")
    entries = []
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as children:
            for child in children:
                path = Path(child.path)
                info = child.stat(follow_symlinks=False)
                entries.append((path.relative_to(root).as_posix(), info.st_size,
                                info.st_mtime_ns, info.st_mode))
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
    return hashlib.sha256(json.dumps(sorted(entries)).encode("ascii")).hexdigest(), len(entries)


def _git(args: list[str], env: dict[str, str], cwd: Path | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, env=env,
                          capture_output=True, check=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("--run", action="append", required=True)
    parser.add_argument("--watch", type=Path, action="append", default=[])
    parser.add_argument("--path", type=Path, action="append", default=[])
    parser.add_argument("--profile", default="default")
    parser.add_argument("--keep", action="store_true")
    report = {"schema_version": 1, "mode": "fresh-clone-check", "profile": "default",
              "commit": None, "commands": [], "watch": [], "kept_path": None, "status": "error"}
    root = args = None
    watched = []
    code = 2
    try:
        with redirect_stderr(StringIO()):
            args = parser.parse_args()
        report["profile"] = args.profile
        if any(not path.is_absolute() or not path.is_dir() for path in args.path):
            raise ValueError("invalid path")
        repo = args.repo.resolve(strict=True)
        for index, path in enumerate(args.watch, 1):
            path = path.resolve(strict=True)
            if path.is_relative_to(repo):
                raise ValueError("invalid watch")
            before, count = _fingerprint(path)
            item = {"index": index, "entries_before": count,
                    "entries_after": None, "changed": False}
            report["watch"].append(item)
            watched.append((path, before, item))
        root = Path(tempfile.mkdtemp(prefix="fresh-clone-check-"))
        (root / "home").mkdir()
        (root / "tmp").mkdir()
        clone = root / "clone"
        env = {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
               "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
               "GIT_CEILING_DIRECTORIES": str(root),
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        _git(["clone", "--quiet", "--no-local", "--single-branch", "--", str(repo), str(clone)], env)
        _git(["remote", "remove", "origin"], env, cwd=clone)
        report["commit"] = _git(["rev-parse", "HEAD"], env, cwd=clone).strip().decode("ascii")
        for path in args.path:
            env["PATH"] += ":" + str(path)
        code, report["status"] = 0, "pass"
        for index, command in enumerate(args.run, 1):
            exit_code = subprocess.run(["/bin/sh", "-c", command], cwd=clone, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
            report["commands"].append({"index": index, "command": command, "exit_code": exit_code})
            if exit_code:
                code, report["status"] = 1, "fail"
                break
    except SystemExit as error:
        if error.code == 0:
            return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        code, report["status"] = 2, "error"
    finally:
        for path, before, item in watched:
            try:
                after, item["entries_after"] = _fingerprint(path)
                item["changed"] = before != after
                if item["changed"]:
                    code = 2
                    if report["status"] != "error":
                        report["status"] = "fail"
            except (OSError, RuntimeError):
                item["changed"] = True
                code, report["status"] = 2, "error"
        if root is not None:
            if args.keep:
                report["kept_path"] = str(root)
            else:
                try:
                    shutil.rmtree(root)
                except OSError:
                    code, report["status"] = 2, "error"
    print(json.dumps(report, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
