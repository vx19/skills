#!/usr/bin/env python3
"""Metadata-only publication pattern gate. Exit codes: 0 clean, 1 review, 2 blocked."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "error: invalid arguments (see --help)\n")


def check(name: str, status: str, categories: dict[str, int]) -> dict[str, object]:
    return {"name": name, "status": status, "finding_count": sum(categories.values()),
            "categories": categories}


def incomplete(name: str) -> dict[str, object]:
    return {"name": name, "status": "incomplete", "finding_count": 0, "categories": {}}


def run_scan(name: str, args: list[str], blocking: bool) -> dict[str, object]:
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().with_name(name + ".py")), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        report = json.loads(result.stdout)
        findings = report.get("findings", [])
        if result.returncode not in (0, 1, 2) or not isinstance(findings, list):
            return incomplete(name)
        categories: Counter[str] = Counter()
        for finding in findings:
            category = "value-overlap" if name == "value_overlap_scan" else finding["category"]
            if not isinstance(category, str):
                return incomplete(name)
            categories[category] += 1
        status = "pass" if result.returncode == 0 else "fail" if blocking else "review"
        if result.returncode == 2:
            status = "incomplete"
        return check(name, status, dict(categories))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return incomplete(name)


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise ValueError
    return result.stdout


def repository_checks(repo: Path, identities: list[str], max_commits: int) -> list[dict[str, object]]:
    checks = []
    try:
        count = int(git(repo, "rev-list", "--all", "--count"))
        categories = {"commit-count": 1} if count > max_commits else {}
        checks.append(check("commit-count", "fail" if categories else "pass", categories))
    except (OSError, ValueError):
        checks.append(incomplete("commit-count"))
    try:
        refs = git(repo, "for-each-ref", "--format=%(refname)").splitlines()
        count = sum(not ref.startswith(b"refs/heads/") for ref in refs)
        categories = {"non-branch-ref": count} if count else {}
        checks.append(check("refs", "fail" if categories else "pass", categories))
    except (OSError, ValueError):
        checks.append(incomplete("refs"))
    try:
        fields = git(repo, "log", "--all", "-z", "--format=%H%x00%an <%ae>%x00%cn <%ce>").split(b"\0")
        if fields[-1] != b"" or (len(fields) - 1) % 3:
            raise ValueError
        allowed = {identity.encode("utf-8") for identity in identities}
        findings = []
        for index in range(0, len(fields) - 1, 3):
            commit_id, author, committer = fields[index:index + 3]
            if not re.fullmatch(rb"[0-9a-f]{40,64}", commit_id):
                raise ValueError
            for role, identity in (("author", author), ("committer", committer)):
                if identity not in allowed:
                    findings.append({"category": "identity-mismatch",
                                     "commit_id": commit_id.decode("ascii"), "role": role})
        categories = {"identity-mismatch": len(findings)} if findings else {}
        entry = check("identities", "fail" if findings else "pass", categories)
        entry["findings"] = findings
        checks.append(entry)
    except (OSError, ValueError, UnicodeError):
        checks.append(incomplete("identities"))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = Parser(description=__doc__)
    parser.add_argument("target", type=Path, help="candidate publication git repository")
    parser.add_argument("--identity", action="append", required=True,
                        help="exact allowed Name <email>; repeatable; an email ending in @users.noreply.github.com is recommended")
    parser.add_argument("--allow-file", type=Path, help="PII allow policy file")
    parser.add_argument("--deny-file", type=Path, help="PII deny policy file")
    parser.add_argument("--source", type=Path, action="append", default=[],
                        help="private value source outside target; repeatable")
    parser.add_argument("--values-file", type=Path, action="append", default=[],
                        help="private values list outside target; repeatable")
    parser.add_argument("--csv-column", action="append", default=[], help="source CSV column; repeatable")
    parser.add_argument("--max-commits", type=int, default=1, help="maximum reachable commits (default: 1)")
    args = parser.parse_args(argv)
    if args.max_commits < 0:
        parser.error("negative commit limit")
    repo = args.target
    checks = repository_checks(repo, args.identity, args.max_commits)
    checks.append(run_scan("safe_repo_scan", [str(repo)], True))
    pii_args = [str(repo), "--history"]
    for option, path in (("--allow-file", args.allow_file), ("--deny-file", args.deny_file)):
        if path is not None:
            pii_args.extend([option, str(path)])
    checks.append(run_scan("pii_scan", pii_args, False))
    if args.source or args.values_file:
        overlap_args = [str(repo)]
        for source in args.source:
            overlap_args.extend(["--source", str(source)])
        for values in args.values_file:
            overlap_args.extend(["--values-file", str(values)])
        for column in args.csv_column:
            overlap_args.extend(["--csv-column", column])
        checks.append(run_scan("value_overlap_scan", overlap_args, False))
    blocked = any(entry["status"] in ("fail", "incomplete") for entry in checks)
    review = any(entry["status"] == "review" for entry in checks)
    gate, status = ("BLOCKED", 2) if blocked else ("REVIEW", 1) if review else ("PATTERN_CLEAN", 0)
    report = {"schema_version": 1, "mode": "publish-gate", "gate": gate, "checks": checks,
              "limitations": ["PATTERN_CLEAN means no pattern matched; contextual review is still required.",
                              "Value overlap is checked only when --source or --values-file is supplied."]}
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
