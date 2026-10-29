#!/usr/bin/env python3
"""Metadata-only PII candidate scan; findings require contextual review."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import safe_repo_scan as safe

# These are candidates, not proof of PII. No probabilistic name recognition.
PATTERNS = (
    ("email", re.compile(rb"(?i)(?<![a-z0-9._%+-])(?!git@[^\s:]+:)(?P<identity>[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})")),
    ("home-identity", re.compile(rb"(?i)(?:/Users/|/home/|[A-Z]:\\Users\\)(?P<identity>[^/\\\s]+)")),
    ("github-account-link", re.compile(rb"(?i)github\.com/(?P<identity>[a-z0-9][a-z0-9-]{0,38})(?=[/\s#?]|$)")),
    ("copyright-holder", re.compile(rb"(?im)\bcopyright\s*(?:\(c\)|\xc2\xa9)?\s*\d{4}(?:\s*[-,]\s*\d{4})?\s+(?P<identity>[^\r\n]+)")),
    ("author-field", re.compile(rb"""(?im)(?<![\w-])["']?(?:author|authors|maintainer|creator)["']?\s*[=:]\s*
        (?:\[\s*|\{\s*["']name["']\s*:\s*)?
        (?P<quote>["'])(?P<identity>[^\r\n]*?)(?P=quote)""", re.X)),
    ("author-field", re.compile(rb"""(?im)^\s*(?:author|maintainer|creator)\s*[=:][ \t]*
        (?![ \t]|["'\[{]|[a-z_]\w*(?:\.\w+)*\s*\()(?P<identity>[^\r\n]+)""", re.X)),
    ("git-ssh-remote", re.compile(rb"(?i)(?<![\w@:/])git@[a-z0-9.-]+:(?P<identity>[a-z0-9_][a-z0-9_.-]*)/[^\s\"'<>]+")),
    ("profile-link", re.compile(rb"""(?ix)(?<![a-z0-9.@-])(?:www\.)?(?:
        (?:gitlab\.com|bitbucket\.org|codeberg\.org|x\.com|twitter\.com)/|
        linkedin\.com/in/|medium\.com/@)(?P<identity>[a-z0-9_][a-z0-9_.-]*)""")),
    ("image-metadata", re.compile(rb"Exif\x00\x00|<x:xmpmeta")),
    ("phone-candidate", re.compile(rb"(?P<identity>\+[1-9][0-9]{0,2}[ .-](?:[0-9][ .-]?){7,14}[0-9])")),
)


def policy_values(path: Path | None) -> set[bytes]:
    if path is None:
        return set()
    values = {line.strip().casefold().encode() for line in path.read_text().splitlines()
              if line.strip() and not line.lstrip().startswith("#")}
    if any(len(value) > 2048 for value in values):
        raise ValueError("identity too long")
    return values


def configure(allowed: set[bytes], denied: set[bytes]) -> None:
    deny_patterns = tuple(("denied-identity", re.compile(re.escape(value), re.I))
                          for value in sorted(denied))
    patterns = deny_patterns + PATTERNS

    def hits(data: bytes):
        for category, pattern in patterns:
            for match in pattern.finditer(data):
                # Allow exact identities only; deny patterns have no identity group.
                if "identity" in pattern.groupindex:
                    value = match.group("identity").strip().strip(b"\"'").lower()
                    if value in allowed:
                        continue
                yield category, match

    def window(data: bytes, absolute_start: int, new_data_start: int, newline_base: int):
        return [(category, absolute_start + match.start(),
                 newline_base + data[:match.start()].count(b"\n") + 1)
                for category, match in hits(data)
                if absolute_start + match.end() > new_data_start]

    safe._scan_window = window
    safe.SECRET_PATTERNS = patterns  # Also redact candidate-shaped filenames.
    safe._secret_shaped_path = lambda path: bool(list(hits(
        path if isinstance(path, bytes) else path.encode("utf-8", "surrogateescape"))))


def scan(root: Path, history: bool) -> tuple[dict, int]:
    if not root.is_dir():
        return {"findings": [], "errors": [{"operation": "open-target"}],
                "summary": {"complete": False}}, 2
    findings, errors, files = safe.scan_worktree(root)
    for finding in findings:
        if finding["category"] == "filename-secret":
            finding["category"] = "filename-identity-candidate"
    blobs = 0
    if history:
        historical, _, history_errors, blobs = safe.scan_history(root)
        findings.extend(historical)
        errors.extend(history_errors)
        # Scan messages and identities as well as historical file blobs.
        result = safe._git(root, ["log", "--all", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x00"])
        if result.returncode:
            errors.append({"operation": "read-commit-identities", "scope": "history"})
        else:
            parts = result.stdout.split(b"\0")
            for i in range(0, len(parts) - 5, 6):
                commit = parts[i].strip()
                if not re.fullmatch(rb"[0-9a-f]{40,64}", commit):
                    errors.append({"operation": "parse-commit-identities", "scope": "history"})
                    break
                for label, value in zip((b"author: ", b"", b"author: ", b"", b""), parts[i+1:i+6]):
                    for category, _, _ in safe._scan_window(label + value, 0, 0, 0):
                        findings.append({"scope": "commit-metadata", "category": category,
                                         "commit_id": commit.decode()})
    # Do not repeat candidates' matched values in diagnostics.
    report = {"schema_version": 1, "mode": "pii-candidates", "findings": findings,
              "errors": errors, "summary": {"complete": not errors,
              "finding_count": len(findings), "worktree_files_checked": files,
              "history_blobs_checked": blobs},
              "limitations": ["Candidates require contextual review; bare names, postal addresses and phones without a leading + may be missed.",
                              "Profile prefixes and Exif/XMP markers are limited candidates; image pixels/OCR are not scanned.",
                              "Symlinks are not followed; inspect exported symlinks separately.",
                              "Only local reachable history is checked when --history is selected."]}
    return report, 2 if errors else 1 if findings else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--allow-file", type=Path, help="local file: one approved public identity per line")
    parser.add_argument("--deny-file", type=Path, help="local file: one private or obsolete identity per line")
    parser.add_argument("--history", action="store_true", help="also scan local reachable blobs and commit metadata")
    args = parser.parse_args()
    try:
        configure(policy_values(args.allow_file), policy_values(args.deny_file))
        report, status = scan(args.target.resolve(), args.history)
    except (OSError, ValueError, UnicodeError):
        report, status = {"errors": [{"operation": "read-policy-or-target"}],
                          "findings": [], "summary": {"complete": False}}, 2
    print(json.dumps(report, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
