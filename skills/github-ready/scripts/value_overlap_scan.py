#!/usr/bin/env python3
"""Find numbers from private data sources in a repository; prints locations, never values.

Secret and identity scans miss a real amount, rate or quantity pasted into a
test or an example. This scan reads the user's private sources (JSON, CSV or
plain text, or a one-value-per-line --values-file, kept outside the target)
and reports each tracked file and line that carries one of their distinctive
numbers. Round figures, dates, repeated or ascending digits (999.99, 12.34) and
long identifiers are ignored: they are everywhere and prove nothing.

Exit 0: no overlap. 1: overlap, review every location. 2: incomplete scan.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

NUMBER = re.compile(r"(?<![\d.])(?:\d{1,3}(?:[,_]\d{3})+|\d+)(?:\.\d+)?(?!\.?\d)")
DATE_LIKE = re.compile(r"(19|20)\d\d(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])?")


def canonical(token: str, min_sig: int, min_int: int) -> str | None:
    try:
        value = abs(Decimal(str(token).replace(",", "").replace("_", "")))
    except InvalidOperation:
        return None
    if not value or not value.is_finite():
        return None
    text = format(value.normalize(), "f")
    digits = text.replace(".", "").strip("0")
    if len(digits) > 15 or len(set(digits)) <= 1 or (len(digits) >= 3 and digits in "0123456789"):
        return None
    if "." in text:
        return text if len(text.replace(".", "").lstrip("0")) >= min_sig else None
    if value < min_int or value % 100 == 0 or DATE_LIKE.fullmatch(text):
        return None
    return text


def numbers(text: str, min_sig: int, min_int: int) -> set[str]:
    return {c for m in NUMBER.finditer(text) if (c := canonical(m.group(), min_sig, min_int))}


def _walk(node: object, out: set[str], min_sig: int, min_int: int) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _walk(value, out, min_sig, min_int)
    elif isinstance(node, list):
        for value in node:
            _walk(value, out, min_sig, min_int)
    elif isinstance(node, str):
        out |= numbers(node, min_sig, min_int)
    elif isinstance(node, (int, Decimal)) and not isinstance(node, bool):
        text = str(node) if isinstance(node, int) else format(node, "f")
        out |= numbers(text, min_sig, min_int)


def load_source(path: Path, columns: set[str], min_sig: int, min_int: int) -> set[str]:
    out: set[str] = set()
    if path.suffix.lower() == ".json":
        _walk(json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal),
              out, min_sig, min_int)
    elif path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                for key, value in row.items():
                    if key is None:
                        raise ValueError("extra CSV fields")
                    if value and (not columns or key in columns):
                        out |= numbers(value, min_sig, min_int)
    else:
        out |= numbers(path.read_text(encoding="utf-8", errors="ignore"), min_sig, min_int)
    return out


def tracked_files(target: Path) -> list[str]:
    proc = subprocess.run(["git", "-C", str(target), "ls-files", "-z"],
                          capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError("git ls-files failed")
    return [raw.decode("utf-8", "surrogateescape") for raw in proc.stdout.split(b"\0") if raw]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", type=Path, help="git work tree to scan (tracked files only)")
    parser.add_argument("--source", type=Path, action="append", default=[],
                        help="private JSON, CSV or text file; repeatable; must be outside TARGET")
    parser.add_argument("--values-file", type=Path, action="append", default=[],
                        help="private values, one per line; blanks/# comments ignored; repeatable; "
                             "must be outside TARGET; requires this or --source. Convert "
                             "scaled-integer stores (cents, shares x10^8) to a plain values file first")
    parser.add_argument("--csv-column", action="append", default=[],
                        help="read only these CSV columns (skip public ones such as market prices)")
    parser.add_argument("--min-sig", type=int, default=4, help="significant digits a decimal needs (default 4)")
    parser.add_argument("--min-int", type=int, default=10_000, help="smallest integer that counts (default 10000)")
    args = parser.parse_args(argv)
    if not args.source and not args.values_file:
        parser.error("a source or values file is required")

    try:
        target = args.target.resolve()
    except (OSError, RuntimeError):
        print(json.dumps({"error": "cannot resolve target"}))
        return 2
    values: set[str] = set()
    for index, source in enumerate(args.source + args.values_file):
        try:
            src = source.resolve()
            if src == target or target in src.parents:
                print(json.dumps({"error": "a source lies inside the target"}))
                return 2
            if index < len(args.source):
                values |= load_source(src, set(args.csv_column), args.min_sig, args.min_int)
            else:
                for line in src.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if c := canonical(line, args.min_sig, args.min_int):
                            values.add(c)
        except (OSError, ValueError, csv.Error, ArithmeticError, RuntimeError):
            print(json.dumps({"error": "unreadable source", "source_index": index}))
            return 2
    try:
        names = tracked_files(target)
    except (OSError, RuntimeError, ValueError):
        print(json.dumps({"error": "cannot read tracked files"}))
        return 2

    findings = []
    for name in names:
        try:
            path = target / name
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
        except OSError:
            print(json.dumps({"error": "cannot read tracked file"}))
            return 2
        if b"\0" in data[:8000]:
            continue
        lines = [n for n, line in enumerate(data.decode("utf-8", "ignore").splitlines(), start=1)
                 if numbers(line, args.min_sig, args.min_int) & values]
        if lines:
            findings.append({"path": name, "lines": lines})
    print(json.dumps({"distinctive_source_values": len(values), "files_scanned": len(names),
                      "findings": findings}, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
