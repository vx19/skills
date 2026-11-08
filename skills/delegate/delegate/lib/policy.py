"""Path globs for scope reporting: detect and label, never block."""

from __future__ import annotations

import fnmatch
import posixpath


def path_matches(path: str, pattern: str) -> bool:
    """True when `path` is `pattern`, lies under it, or matches it as a glob.

    Both are normalised first, so `./src/` is `src` and `.` (or empty) is the
    whole tree; a name such as `.src` is never mistaken for `src`.
    """
    path, pat = posixpath.normpath(path), posixpath.normpath(pattern or ".")
    return (pat == "." or path == pat or path.startswith(pat + "/")
            or fnmatch.fnmatch(path, pat))


def violations(paths: list[str], allow: list[str]) -> list[str]:
    return [p for p in paths if not any(path_matches(p, a) for a in allow)]
