#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

usage() {
  cat <<'EOF'
usage: publish_snapshot.sh --identity "Name <email>" --out DIR
    [--branch NAME] [--exclude-file F] [--remote-url URL]
    [--accept-review] [-- GATE-ARGS...]
PUBLISH_GATE defaults to publish_gate.py beside this script.
EOF
}

die() { echo "publish-snapshot: $*" >&2; exit 1; }

branch=publish identity= out= exclude_file=
remote_url='<public-remote-url>' accept_review=0
gate_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --identity|--out|--branch|--exclude-file|--remote-url)
      [ $# -ge 2 ] && [ -n "$2" ] || { usage >&2; exit 2; }
      case "$1" in
        --identity) identity=$2 ;;
        --out) out=$2 ;;
        --branch) branch=$2 ;;
        --exclude-file) exclude_file=$2 ;;
        --remote-url) remote_url=$2 ;;
      esac
      shift 2 ;;
    --accept-review) accept_review=1; shift ;;
    --) shift; gate_args=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[ -n "$identity" ] && [ -n "$out" ] || { usage >&2; exit 2; }

# No inherited repository override may point operations in the clone at source.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_NAMESPACE
unset GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_SHALLOW_FILE
export GIT_NO_REPLACE_OBJECTS=1
repo=$(git rev-parse --show-toplevel 2>/dev/null) || die "not a worktree"
repo=$(cd "$repo" && pwd -P)
cd "$repo"
head=$(git rev-parse --verify --quiet 'HEAD^{commit}') || die "HEAD has no commit"
status=$(git --no-optional-locks status --porcelain --untracked-files=no 2>/dev/null) ||
  die "cannot inspect tracked status"
[ -z "$status" ] || die "tracked files have uncommitted changes"
valid_branch=$(git check-ref-format --branch "$branch" 2>/dev/null) || die "invalid branch name"
[ "$valid_branch" = "$branch" ] || die "invalid branch name"
ref=refs/heads/$branch
while IFS= read -r entry; do
  [ "$entry" != "branch $ref" ] || die "snapshot branch is checked out"
done < <(git worktree list --porcelain)

identity_parts=$(python3 - "$identity" <<'PY'
import re
import sys

match = re.fullmatch(r'([^<>\r\n\x00-\x1f\x7f]+) <([^<>\s@\x00-\x1f\x7f]+@[^<>\s@\x00-\x1f\x7f]+)>', sys.argv[1])
if not match or match[1] != match[1].strip():
    sys.exit(1)
print('\n'.join(match.groups()))
PY
) || die "identity must have the form Name <email>"
identity_name=${identity_parts%%$'\n'*}
identity_email=${identity_parts#*$'\n'}

gate=${PUBLISH_GATE:-$script_dir/publish_gate.py}
[ -f "$gate" ] || die "gate missing; set PUBLISH_GATE to the skill's scripts/publish_gate.py"

previous=$(git rev-parse --verify --quiet "$ref" || true)
if [ -n "$previous" ]; then
  [ "$(git rev-list --parents -n 1 "$previous")" = "$previous" ] ||
    die "existing branch is not a parentless snapshot"
fi

# Resolve symlinks before any deletion, including symlinks in missing parents.
out=$(python3 - "$repo" "$out" "$(git rev-parse --git-common-dir)" <<'PYTHON'
import os
import sys

out = os.path.realpath(sys.argv[2])
for path in (sys.argv[1], os.path.realpath(sys.argv[3])):
    if os.path.commonpath((out, path)) in (out, path):
        sys.exit(1)
print(out)
PYTHON
) || die "unsafe output directory"
if [ -e "$out" ]; then
  marker=$(git -C "$out" config --local --get publish-snapshot.source 2>/dev/null || true)
  [ "$marker" = "$repo" ] && [ -d "$out/.git" ] || die "output is not a previous snapshot clone"
fi

tmp=$(mktemp -d "${TMPDIR:-/tmp}/publish-snapshot.XXXXXX")
commit= updated=0 out_owned=0 passed=0
finish() {
  rc=$?
  trap - EXIT INT TERM
  set +e
  if [ "$passed" != 1 ]; then
    if [ "$out_owned" = 1 ]; then rm -rf -- "$out"; fi
    if [ "$updated" = 1 ]; then
      if [ -n "$previous" ]; then
        git -C "$repo" update-ref "$ref" "$previous" "$commit" 2>/dev/null || true
      else
        git -C "$repo" update-ref -d "$ref" "$commit" 2>/dev/null || true
      fi
    fi
  fi
  rm -rf -- "$tmp"
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

tree=$(git rev-parse "$head^{tree}")
if [ -n "$exclude_file" ]; then
  python3 - "$exclude_file" "$tmp/excludes" <<'PY' || die "invalid exclude file"
import posixpath
import sys

try:
    with open(sys.argv[1], encoding='utf-8') as source, open(sys.argv[2], 'wb') as target:
        for line in source:
            path = line.rstrip('\n')  # text mode already maps CRLF to LF
            if not path.strip() or path.lstrip().startswith('#'):
                continue
            if path.startswith('/') or '..' in path.split('/') or '\x00' in path or posixpath.normpath(path) == '.':
                sys.exit(1)
            target.write(posixpath.normpath(path).encode('utf-8') + b'\x00')
except (OSError, UnicodeError):
    sys.exit(1)
PY
  GIT_INDEX_FILE=$tmp/index git read-tree "$head" 2>/dev/null
  if [ -s "$tmp/excludes" ]; then
    GIT_INDEX_FILE=$tmp/index GIT_LITERAL_PATHSPECS=1 \
      git rm --cached -r --quiet --ignore-unmatch \
      --pathspec-from-file="$tmp/excludes" --pathspec-file-nul 2>/dev/null
  fi
  tree=$(GIT_INDEX_FILE=$tmp/index git write-tree 2>/dev/null)
fi

head_date=$(git log -1 --format=%cd --date=raw "$head")
commit=$(GIT_AUTHOR_NAME=$identity_name GIT_AUTHOR_EMAIL=$identity_email \
  GIT_COMMITTER_NAME=$identity_name GIT_COMMITTER_EMAIL=$identity_email \
  GIT_AUTHOR_DATE=$head_date GIT_COMMITTER_DATE=$head_date \
  git commit-tree "$tree" -m "Initial import" 2>/dev/null)
[ "$(git show -s --format='%an <%ae>' "$commit")" = "$identity" ] &&
  [ "$(git show -s --format='%cn <%ce>' "$commit")" = "$identity" ] ||
  die "snapshot identity verification failed"
updated=1
git update-ref "$ref" "$commit" "$previous" 2>/dev/null || die "snapshot branch changed concurrently"

out_owned=1
rm -rf -- "$out"
git clone --quiet --no-local --no-tags --single-branch --branch "$branch" \
  -- "$repo" "$out" 2>/dev/null || die "isolated clone failed"
git -C "$out" remote remove origin 2>/dev/null
git -C "$out" config publish-snapshot.source "$repo" 2>/dev/null
[ "$(git -C "$out" rev-parse HEAD)" = "$commit" ] || die "clone tip differs from snapshot"

rc=0
python3 "$gate" "$out" --identity "$identity" --max-commits 1 \
  "${gate_args[@]+"${gate_args[@]}"}" > "$tmp/gate.out" 2> "$tmp/gate.err" || rc=$?
# Parsed check summary only (names, statuses, category counts); raw output is never echoed.
python3 -c 'import json, sys
checks = json.load(open(sys.argv[1]))["checks"]
for c in checks:
    print("  %s: %s %s" % (c["name"], c["status"], json.dumps(c["categories"], sort_keys=True)))
if not any(c["name"] == "value_overlap_scan" for c in checks):
    print("  value-overlap: skipped (no --source or --values-file)")' \
  "$tmp/gate.out" 2>/dev/null || die "gate output unreadable"
case "$rc" in
  0) echo "gate: PASS" ;;
  1) [ "$accept_review" = 1 ] || die "gate: REVIEW (requires --accept-review after human review)"
     echo "gate: REVIEW accepted" ;;
  *) die "gate: FAIL (exit $rc)" ;;
esac

files=$(git -C "$out" ls-tree -r -z --name-only HEAD | python3 -c 'import sys; print(sys.stdin.buffer.read().count(b"\x00"))')
printf 'commit: %s\nfiles: %s\ntree: %s\nout: %s\n' "$commit" "$files" "$tree" "$out"
printf 'git -C %q push --force-with-lease=main:%s %q %q\n' "$out" "$previous" "$remote_url" "$branch:main"
printf 'git ls-remote %q main  # must print %s\n' "$remote_url" "$commit"
passed=1
