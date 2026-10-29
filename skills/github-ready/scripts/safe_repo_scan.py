#!/usr/bin/env python3
"""Read-only repository secret and large-object scanner with metadata-only output.

Exit codes: 0 clean and complete, 1 suspected secrets, 2 incomplete (takes precedence).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable


GITHUB_WARNING_BYTES = 50 * 1024 * 1024
GITHUB_BLOCK_BYTES = 100 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024
_OVERLAP = 4096

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("aws-access-key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(rb"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("gitlab-token", re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(rb"\bxoxb-[A-Za-z0-9-]{20,}\b")),
    ("api-token", re.compile(rb"\b(?:sk-ant-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{20,})\b")),
    (
        "private-key",
        re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    ),
    (
        "credential-assignment",
        re.compile(
            rb"(?i)\b(?:api[_-]?key|secret|token|password)\b[^\r\n]{0,48}?"
            rb"[=:]\s*['\"]?[A-Za-z0-9+/][A-Za-z0-9+/_-]{23,}={0,2}"
        ),
    ),
    (
        "credential-url",
        re.compile(
            rb"(?i)\b(?:postgres(?:ql)?|mongodb(?:\+srv)?|redis)://"
            rb"[^\s/:@]+:[^\s/@]+@"
        ),
    ),
    ("bearer-token", re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}={0,2}")),
)

_PLACEHOLDERS = (
    b"your_api_key",
    b"changeme",
    b"not-a-real-secret",
    b"[redacted]",
)
_SENSITIVE_PATH_PART = re.compile(
    r"(?i)(?:^\.env(?:\.|$)|id_(?:rsa|dsa|ecdsa|ed25519)$|"
    r"(?:secret|token|password|credential|private[-_]?key))"
)
_HUNK = re.compile(rb"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _git(repo: Path, args: list[str], input_data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    command = ["git", *args]
    try:
        return subprocess.run(
            command,
            cwd=repo,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=env,
        )
    except OSError:
        # Preserve the operation failure without forwarding exception text, paths,
        # command arguments, or child-process diagnostics to output.
        return subprocess.CompletedProcess(command, 127, b"", b"")


def _safe_path(raw_path: str | bytes | None) -> tuple[str | None, bool]:
    if raw_path is None:
        return None, False
    if isinstance(raw_path, bytes):
        try:
            value = raw_path.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return None, True
    else:
        value = raw_path
    normalized = value.replace(os.sep, "/")
    path = PurePosixPath(normalized)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or "@" in value
        or _SENSITIVE_PATH_PART.search(value)
        or any(pattern.search(value.encode("utf-8")) for _, pattern in SECRET_PATTERNS)
    ):
        return None, True
    return str(path), False


def _secret_shaped_path(raw_path: str | bytes | None) -> bool:
    if raw_path is None:
        return False
    data = raw_path if isinstance(raw_path, bytes) else raw_path.encode("utf-8", "surrogateescape")
    try:
        value = data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return False
    return bool(
        _SENSITIVE_PATH_PART.search(value)
        or any(
            any(not _is_placeholder(match.group(0)) for match in pattern.finditer(data))
            for _, pattern in SECRET_PATTERNS
        )
    )


def _is_placeholder(match: bytes) -> bool:
    lowered = match.lower()
    return any(marker in lowered for marker in _PLACEHOLDERS)


def _scan_window(data: bytes, absolute_start: int, new_data_start: int, newline_base: int) -> list[tuple[str, int, int]]:
    hits: list[tuple[str, int, int]] = []
    for category, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(data):
            if absolute_start + match.end() <= new_data_start or _is_placeholder(match.group(0)):
                continue
            line = newline_base + data[: match.start()].count(b"\n") + 1
            hits.append((category, absolute_start + match.start(), line))
    return hits


def scan_stream(stream: BinaryIO) -> tuple[list[tuple[str, int]], bool]:
    """Return category/line metadata and whether the stream appears binary."""
    carry = b""
    total_bytes = 0
    total_newlines = 0
    binary = False
    collected: dict[tuple[str, int], int] = {}
    while True:
        chunk = stream.read(_CHUNK_SIZE)
        if not chunk:
            break
        binary = binary or b"\0" in chunk
        combined = carry + chunk
        combined_start = total_bytes - len(carry)
        newline_base = total_newlines - carry.count(b"\n")
        for category, offset, line in _scan_window(combined, combined_start, total_bytes, newline_base):
            collected[(category, offset)] = line
        total_bytes += len(chunk)
        total_newlines += chunk.count(b"\n")
        carry = combined[-_OVERLAP:]
    return sorted(((category, line) for (category, _), line in collected.items())), binary


def _finding(scope: str, category: str, path: str | bytes | None, line: int | None = None, **extra: object) -> dict[str, object]:
    finding: dict[str, object] = {"category": category, "scope": scope}
    safe, redacted = _safe_path(path)
    if safe is not None:
        finding["path"] = safe
    if redacted:
        finding["path_redacted"] = True
    if line is not None:
        finding["line"] = line
    finding.update(extra)
    return finding


def _scan_file(path: Path, relative: str, scope: str) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    try:
        with path.open("rb") as stream:
            hits, binary = scan_stream(stream)
    except OSError:
        return [], {"category": "scan-error", "operation": "read-file", "scope": scope, **_path_fields(relative)}
    findings = [
        _finding(scope, category, relative, line, content_kind="binary" if binary else "text")
        for category, line in hits
    ]
    return findings, None


def _path_fields(path: str | bytes | None) -> dict[str, object]:
    safe, redacted = _safe_path(path)
    result: dict[str, object] = {}
    if safe is not None:
        result["path"] = safe
    if redacted:
        result["path_redacted"] = True
    return result


def scan_worktree(repo: Path) -> tuple[list[dict[str, object]], list[dict[str, object]], int]:
    findings: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    checked = 0
    for directory, dirnames, filenames in os.walk(repo, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        for filename in sorted(filenames):
            path = Path(directory, filename)
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(repo).as_posix()
            if _secret_shaped_path(relative):
                findings.append(_finding("worktree", "filename-secret", relative))
            file_findings, error = _scan_file(path, relative, "worktree")
            checked += 1
            findings.extend(file_findings)
            if error:
                errors.append(error)
    return findings, errors, checked


def _reachable_blob_paths(
    repo: Path,
) -> tuple[dict[str, list[bytes]], list[dict[str, object]]]:
    revisions = _git(repo, ["rev-list", "--all"])
    if revisions.returncode:
        return {}, [
            {
                "category": "scan-error",
                "operation": "list-reachable-commits",
                "scope": "history",
                "exit_code": revisions.returncode,
            }
        ]
    collected: dict[str, set[bytes]] = {}
    errors: list[dict[str, object]] = []
    for raw_commit in sorted(revisions.stdout.splitlines()):
        try:
            commit_id = raw_commit.decode("ascii", "strict")
        except UnicodeDecodeError:
            continue
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit_id):
            continue
        tree = _git(repo, ["ls-tree", "-r", "-z", "--full-tree", commit_id])
        if tree.returncode:
            errors.append(
                {
                    "category": "scan-error",
                    "commit_id": commit_id,
                    "operation": "list-commit-tree",
                    "scope": "history",
                    "exit_code": tree.returncode,
                }
            )
            continue
        for record in tree.stdout.split(b"\0"):
            metadata, separator, path = record.partition(b"\t")
            fields = metadata.split()
            if not separator or len(fields) != 3 or fields[1] != b"blob":
                continue
            try:
                object_id = fields[2].decode("ascii", "strict")
            except UnicodeDecodeError:
                continue
            if re.fullmatch(r"[0-9a-f]{40,64}", object_id):
                collected.setdefault(object_id, set()).add(path)
    return {object_id: sorted(paths) for object_id, paths in collected.items()}, errors


def _reachable_objects(repo: Path) -> tuple[list[tuple[str, str, int]], dict[str, list[bytes]], list[dict[str, object]]]:
    listed = _git(repo, ["rev-list", "--objects", "--all"])
    if listed.returncode:
        return [], {}, [{"category": "scan-error", "operation": "list-reachable-objects", "scope": "history", "exit_code": listed.returncode}]
    object_ids: list[str] = []
    for raw_line in listed.stdout.splitlines():
        object_id = raw_line.partition(b" ")[0]
        try:
            decoded_id = object_id.decode("ascii")
        except UnicodeDecodeError:
            continue
        if not re.fullmatch(r"[0-9a-f]{40,64}", decoded_id):
            continue
        object_ids.append(decoded_id)
    paths, path_errors = _reachable_blob_paths(repo)
    metadata = _git(
        repo,
        ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        ("\n".join(object_ids) + "\n").encode("ascii"),
    )
    if metadata.returncode:
        return [], paths, path_errors + [{"category": "scan-error", "operation": "inspect-reachable-objects", "scope": "history", "exit_code": metadata.returncode}]
    objects: list[tuple[str, str, int]] = []
    for line in metadata.stdout.decode("ascii", "replace").splitlines():
        fields = line.split()
        if len(fields) != 3 or not fields[2].isdigit():
            continue
        objects.append((fields[0], fields[1], int(fields[2])))
    return objects, paths, path_errors


def _scan_blob(repo: Path, object_id: str) -> tuple[list[tuple[str, int]], bool, dict[str, object] | None]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    try:
        process = subprocess.Popen(
            ["git", "cat-file", "blob", object_id],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except OSError:
        return [], False, {"category": "scan-error", "operation": "open-history-blob", "scope": "history", "blob_id": object_id}
    assert process.stdout is not None
    try:
        hits, binary = scan_stream(process.stdout)
    except OSError:
        process.kill()
        process.wait()
        return [], False, {"category": "scan-error", "operation": "read-history-blob", "scope": "history", "blob_id": object_id}
    returncode = process.wait()
    if returncode:
        return [], binary, {"category": "scan-error", "operation": "read-history-blob", "scope": "history", "blob_id": object_id, "exit_code": returncode}
    return hits, binary, None


def classify_blob_size(size: int) -> str | None:
    if size > GITHUB_BLOCK_BYTES:
        return "block"
    if size > GITHUB_WARNING_BYTES:
        return "warning"
    return None


def scan_history(
    repo: Path,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    int,
]:
    objects, paths, errors = _reachable_objects(repo)
    findings: list[dict[str, object]] = []
    large_blobs: list[dict[str, object]] = []
    checked = 0
    for object_id, object_type, size in objects:
        if object_type != "blob":
            continue
        checked += 1
        object_paths = paths.get(object_id, [])
        for object_path in object_paths:
            if _secret_shaped_path(object_path):
                findings.append(
                    _finding(
                        "history",
                        "filename-secret",
                        object_path,
                        blob_id=object_id,
                    )
                )
        severity = classify_blob_size(size)
        if severity:
            item: dict[str, object] = {
                "blob_id": object_id,
                "category": "large-blob",
                "scope": "history",
                "severity": severity,
                "size_bytes": size,
            }
            item.update(_path_fields(object_paths[0] if object_paths else None))
            large_blobs.append(item)
        hits, binary, error = _scan_blob(repo, object_id)
        if error:
            errors.append(error)
            continue
        for category, line in hits:
            findings.append(
                _finding(
                    "history",
                    category,
                    object_paths[0] if object_paths else None,
                    line,
                    blob_id=object_id,
                    content_kind="binary" if binary else "text",
                )
            )
    return findings, large_blobs, errors, checked


def _nul_paths(result: subprocess.CompletedProcess[bytes]) -> list[bytes]:
    return sorted(path for path in result.stdout.split(b"\0") if path)


def _change(scope: str, state: str, path: bytes) -> dict[str, object]:
    return {"scope": scope, "state": state, **_path_fields(path)}


def _parse_name_status(scope: str, data: bytes) -> list[dict[str, object]]:
    state_names = {
        b"A": "added",
        b"D": "deleted",
        b"M": "modified",
        b"T": "type-changed",
        b"U": "unmerged",
        b"X": "unknown",
        b"B": "pairing-broken",
    }
    tokens = [token for token in data.split(b"\0") if token]
    changes: list[dict[str, object]] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        code = status[:1]
        path_count = 2 if code in {b"R", b"C"} else 1
        if index + path_count > len(tokens):
            break
        paths = tokens[index : index + path_count]
        index += path_count
        if code == b"R":
            changes.extend(
                [
                    _change(scope, "renamed-from", paths[0]),
                    _change(scope, "renamed-to", paths[1]),
                ]
            )
        elif code == b"C":
            changes.extend(
                [
                    _change(scope, "copied-from", paths[0]),
                    _change(scope, "copied-to", paths[1]),
                ]
            )
        else:
            changes.append(_change(scope, state_names.get(code, "unknown"), paths[0]))
    return changes


def _head_metadata(
    repo: Path,
) -> tuple[dict[str, object], dict[str, object] | None]:
    branch = _git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if branch.returncode == 0:
        raw_branch = branch.stdout.rstrip(b"\r\n")
        safe, redacted = _safe_path(raw_branch)
        metadata: dict[str, object] = {"detached": False}
        if safe is not None:
            metadata["branch"] = safe
        if redacted:
            metadata["branch_redacted"] = True
        return metadata, None
    if branch.returncode != 1:
        return {"detached": None}, {
            "category": "scan-error",
            "operation": "inspect-head-state",
            "scope": "repository",
            "exit_code": branch.returncode,
        }
    head = _git(repo, ["rev-parse", "--verify", "HEAD"])
    commit_id = head.stdout.strip().decode("ascii", "replace")
    if head.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", commit_id):
        return {"detached": True}, {
            "category": "scan-error",
            "operation": "inspect-detached-head",
            "scope": "repository",
            "exit_code": head.returncode,
        }
    return {"detached": True, "head_commit": commit_id}, None


def _scan_changed_lines(
    patch: bytes, path: bytes, scope: str
) -> tuple[list[dict[str, object]], int, int]:
    findings: list[dict[str, object]] = []
    old_line: int | None = None
    new_line: int | None = None
    added_checked = 0
    removed_checked = 0
    for line in patch.splitlines():
        hunk = _HUNK.match(line)
        if hunk:
            old_line = int(hunk.group(1))
            new_line = int(hunk.group(2))
            continue
        if old_line is None or new_line is None:
            continue
        if line.startswith(b"+"):
            content = line[1:]
            for category, pattern in SECRET_PATTERNS:
                if any(not _is_placeholder(match.group(0)) for match in pattern.finditer(content)):
                    findings.append(
                        _finding(
                            scope,
                            category,
                            path,
                            new_line,
                            change_kind="added",
                            content_kind="text",
                        )
                    )
            added_checked += 1
            new_line += 1
        elif line.startswith(b"-"):
            content = line[1:]
            for category, pattern in SECRET_PATTERNS:
                if any(not _is_placeholder(match.group(0)) for match in pattern.finditer(content)):
                    findings.append(
                        _finding(
                            scope,
                            category,
                            path,
                            old_line,
                            change_kind="removed",
                            content_kind="text",
                        )
                    )
            removed_checked += 1
            old_line += 1
        elif not line.startswith(b"\\"):
            old_line += 1
            new_line += 1
    return findings, added_checked, removed_checked


def scan_diff(
    repo: Path,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    int,
    int,
    int,
]:
    errors: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    changes: list[dict[str, object]] = []
    path_sets: list[tuple[str, list[str], str]] = [
        ("diff-unstaged", ["diff", "--name-only", "-z", "--no-ext-diff"], "unstaged"),
        ("diff-staged", ["diff", "--cached", "--name-only", "-z", "--no-ext-diff"], "staged"),
        ("diff-untracked", ["ls-files", "--others", "--exclude-standard", "-z"], "untracked"),
    ]
    checked_paths: set[bytes] = set()
    added_lines_checked = 0
    removed_lines_checked = 0
    for scope, command, kind in path_sets:
        result = _git(repo, command)
        if result.returncode:
            errors.append({"category": "scan-error", "operation": f"list-{kind}-files", "scope": scope, "exit_code": result.returncode})
            continue
        raw_paths = _nul_paths(result)
        if kind == "untracked":
            changes.extend(_change(scope, "untracked", path) for path in raw_paths)
        else:
            status_args = ["diff", "--name-status", "-z", "--no-ext-diff"]
            if kind == "staged":
                status_args.append("--cached")
            status_result = _git(repo, status_args)
            if status_result.returncode:
                errors.append(
                    {
                        "category": "scan-error",
                        "operation": f"inspect-{kind}-states",
                        "scope": scope,
                        "exit_code": status_result.returncode,
                    }
                )
            else:
                changes.extend(_parse_name_status(scope, status_result.stdout))
        for raw_path in raw_paths:
            checked_paths.add(raw_path)
            if _secret_shaped_path(raw_path):
                findings.append(
                    _finding(
                        scope,
                        "filename-secret",
                        raw_path,
                        change_kind="path",
                    )
                )
            if kind == "untracked":
                try:
                    relative = raw_path.decode("utf-8", "strict")
                    candidate = repo / relative
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    with candidate.open("rb") as stream:
                        hits, binary = scan_stream(stream)
                    for category, line in hits:
                        findings.append(
                            _finding(
                                scope,
                                category,
                                raw_path,
                                line,
                                change_kind="added",
                                content_kind="binary" if binary else "text",
                            )
                        )
                except (UnicodeDecodeError, OSError):
                    errors.append({"category": "scan-error", "operation": "read-untracked-file", "scope": scope, **_path_fields(raw_path)})
                continue
            diff_args = ["diff", "--unified=0", "--no-ext-diff", "--no-textconv", "--no-color"]
            if kind == "staged":
                diff_args.append("--cached")
            diff_args.extend(["--", os.fsdecode(raw_path)])
            patch = _git(repo, diff_args)
            if patch.returncode:
                errors.append({"category": "scan-error", "operation": f"read-{kind}-diff", "scope": scope, "exit_code": patch.returncode, **_path_fields(raw_path)})
                continue
            changed_findings, added_count, removed_count = _scan_changed_lines(
                patch.stdout, raw_path, scope
            )
            findings.extend(changed_findings)
            added_lines_checked += added_count
            removed_lines_checked += removed_count
            if b"Binary files " in patch.stdout or b"GIT binary patch" in patch.stdout:
                try:
                    relative = raw_path.decode("utf-8", "strict")
                    with (repo / relative).open("rb") as stream:
                        hits, binary = scan_stream(stream)
                    for category, line in hits:
                        findings.append(
                            _finding(
                                f"{scope}-file",
                                category,
                                raw_path,
                                line,
                                change_kind="current-file",
                                content_kind="binary" if binary else "text",
                            )
                        )
                except (UnicodeDecodeError, OSError):
                    errors.append({"category": "scan-error", "operation": "read-binary-change", "scope": scope, **_path_fields(raw_path)})
    unique_changes = {
        json.dumps(change, sort_keys=True, separators=(",", ":")): change
        for change in changes
    }
    return (
        findings,
        errors,
        list(unique_changes.values()),
        len(checked_paths),
        added_lines_checked,
        removed_lines_checked,
    )


def _sort_items(items: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))


def build_report(repo: Path, diff_safety: bool) -> tuple[dict[str, object], int]:
    probe = _git(repo, ["rev-parse", "--is-inside-work-tree"])
    if probe.returncode or probe.stdout.strip() != b"true":
        report = {
            "errors": [{"category": "scan-error", "operation": "verify-git-repository", "scope": "repository", "exit_code": probe.returncode}],
            "findings": [],
            "mode": "diff-safety" if diff_safety else "repository",
            "schema_version": 1,
            "summary": {"complete": False, "finding_count": 0, "suspected_secrets": False},
        }
        return report, 2
    if diff_safety:
        findings, errors, changes, files_checked, added_checked, removed_checked = scan_diff(repo)
        head, head_error = _head_metadata(repo)
        if head_error:
            errors.append(head_error)
        report = {
            "changes": _sort_items(changes),
            "errors": _sort_items(errors),
            "findings": _sort_items(findings),
            "head": head,
            "mode": "diff-safety",
            "schema_version": 1,
            "summary": {
                "added_lines_checked": added_checked,
                "changed_files_checked": files_checked,
                "changed_lines_checked": added_checked + removed_checked,
                "complete": not errors,
                "finding_count": len(findings),
                "removed_lines_checked": removed_checked,
                "suspected_secrets": bool(findings),
            },
        }
        return report, 2 if errors else 1 if findings else 0
    worktree_findings, worktree_errors, worktree_checked = scan_worktree(repo)
    history_findings, large_blobs, history_errors, blobs_checked = scan_history(repo)
    findings = worktree_findings + history_findings
    errors = worktree_errors + history_errors
    report = {
        "errors": _sort_items(errors),
        "findings": _sort_items(findings),
        "large_blobs": _sort_items(large_blobs),
        "mode": "repository",
        "schema_version": 1,
        "summary": {
            "complete": not errors,
            "finding_count": len(findings),
            "history_blobs_checked": blobs_checked,
            "large_blob_count": len(large_blobs),
            "suspected_secrets": bool(findings),
            "worktree_files_checked": worktree_checked,
        },
        "thresholds": {
            "github_block_bytes": GITHUB_BLOCK_BYTES,
            "github_warning_bytes": GITHUB_WARNING_BYTES,
        },
    }
    return report, 2 if errors else 1 if findings else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan a local Git repository without emitting file contents or secret values.",
        epilog="Exit codes: 0 clean and complete, 1 suspected secrets, 2 incomplete (takes precedence in both modes).",
    )
    parser.add_argument("repo", nargs="?", default=".", help="repository to scan (default: current directory)")
    parser.add_argument(
        "--diff-safety",
        action="store_true",
        help="scan changed diff lines and untracked files without printing content",
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        report = {
            "errors": [{"category": "scan-error", "operation": "open-repository", "scope": "repository"}],
            "findings": [],
            "mode": "diff-safety" if args.diff_safety else "repository",
            "schema_version": 1,
            "summary": {"complete": False, "finding_count": 0, "suspected_secrets": False},
        }
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 2
    report, status = build_report(repo, args.diff_safety)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
