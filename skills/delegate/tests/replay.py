#!/usr/bin/env python3
"""Replay a synthetic recorded run through the real `bin/delegate run`.

A fixture directory holds:

    meta.json     {"provider": "codex", "exit": 1}
    stream.jsonl  the runner's stdout, one JSON event per line
    runner.log    the runner's stderr

Fixtures are synthetic. Recorded runs are only a source for event shapes and
the decisive error lines; recorded project content never lands in this repo.

The helper creates a throwaway directory tree (fake runner, scratch git repo,
state root, HOME), puts the fake runner first on PATH, then invokes the real
CLI. Nothing touches the operator's `~/.local/state`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DELEGATE = REPO_ROOT / "bin" / "delegate"

# provider -> executable name the real provider adapter looks for on PATH.
RUNNERS = {
    "grok": "grok",
    "claude": "claude",
    "codex": "codex",
    "cursor": "cursor-agent",
    "pi": "pi",
}


def _fixture_parts(fixture: Path) -> tuple[str, int]:
    meta = json.loads((fixture / "meta.json").read_text(encoding="utf-8"))
    provider = meta["provider"]
    if provider not in RUNNERS:
        raise ValueError(f"unknown provider in {fixture}/meta.json: {provider!r}")
    return provider, int(meta.get("exit", 1))


def _fake_source(fixture: Path, sleep: float, exit_code: int) -> str:
    """A runner that ignores its argv and re-emits one fixture, in order."""
    stream = str((fixture / "stream.jsonl").resolve())
    runner_log = str((fixture / "runner.log").resolve())
    return f"""#!/usr/bin/env python3
import sys
import time

stream = {stream!r}
runner_log = {runner_log!r}
sleep = {float(sleep)!r}

with open(stream, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        sys.stdout.write(line)
        sys.stdout.flush()
        if sleep:
            time.sleep(sleep)
with open(runner_log, encoding="utf-8", errors="replace") as fh:
    sys.stderr.write(fh.read())
sys.stderr.flush()
sys.exit({exit_code})
"""


def _write_fakes(bindir: Path, names: set[str], source: str) -> None:
    for name in sorted(names):
        path = bindir / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)


def _scratch_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=replay",
         "-c", "user.email=replay", "commit", "-qm", "initial"],
        check=True, capture_output=True)
    return repo


def replay(fixture: str | Path, sleep: float = 0, extra=(),
           artifacts: dict | None = None) -> dict:
    """Run one fixture through the real CLI and return its envelope.

    `extra` is a tuple of extra `delegate run` arguments (flags only). The
    fixture's provider chooses the fake runner's name; `exit` is its status.
    When `artifacts` is a dict, the run's `progress.json` and `result.json`
    are copied into it before the temp state tree is removed.
    """
    fixture = Path(fixture).resolve()
    provider, exit_code = _fixture_parts(fixture)

    with tempfile.TemporaryDirectory(prefix="delegate-replay-") as tmp:
        root = Path(tmp)
        bindir = root / "bin"
        bindir.mkdir()
        names = {provider}
        names.add(RUNNERS[provider])
        _write_fakes(bindir, names, _fake_source(fixture, sleep, exit_code))
        repo = _scratch_repo(root)
        state = root / "state"
        state.mkdir()
        home = root / "home"
        home.mkdir()

        env = dict(os.environ)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        env["XDG_STATE_HOME"] = str(state)
        env["HOME"] = str(home)
        env["XAI_API_KEY"] = "fake"       # grok's offline preflight
        # pi probes ~/.pi/agent; HOME above is the throwaway one.
        env.pop("PI_CODING_AGENT_DIR", None)
        env.pop("GROK_AGENT", None)

        argv = [str(DELEGATE), "run", "--provider", provider,
                "--cwd", str(repo), "--quiet", *extra, "--", "replay"]
        proc = subprocess.run(argv, cwd=str(REPO_ROOT), env=env, text=True,
                              capture_output=True)
        raw = proc.stdout.strip()
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(
                "delegate produced no envelope "
                f"(exit {proc.returncode})\nstdout:\n{proc.stdout}\n"
                f"stderr:\n{proc.stderr}") from None
        if not isinstance(envelope, dict):
            raise RuntimeError(f"delegate envelope was not an object: {envelope!r}")
        if artifacts is not None:
            run_dir = state / "delegate" / str(envelope.get("runId"))
            for name in ("progress.json", "result.json"):
                try:
                    artifacts[name] = (run_dir / name).read_text(encoding="utf-8")
                except OSError:
                    pass
        return envelope


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        split = argv.index("--")
        extra = tuple(argv[split + 1:])
        argv = argv[:split]
    else:
        extra = ()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fixture", help="fixture directory")
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="seconds between stream lines")
    args = parser.parse_args(argv)
    envelope = replay(args.fixture, sleep=args.sleep, extra=extra)
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())