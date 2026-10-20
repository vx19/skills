"""Path globs for scope reporting: detect and label, never block."""

from __future__ import annotations

import fnmatch


def path_matches(path: str, pattern: str) -> bool:
    path_n = path.replace("\\", "/").lstrip("./")
    pat = pattern.replace("\\", "/").lstrip("./")
    if not pat:
        return False
    if pat.endswith("/"):
        return path_n == pat[:-1] or path_n.startswith(pat)
    if fnmatch.fnmatch(path_n, pat):
        return True
    # directory-prefix convenience: "src" matches "src/foo.py"
    return path_n == pat or path_n.startswith(pat + "/")


def violations(paths: list[str], allow: list[str], deny: list[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if deny and any(path_matches(p, d) for d in deny):
            out.append(p)
            continue
        if allow and not any(path_matches(p, a) for a in allow):
            out.append(p)
    return out
