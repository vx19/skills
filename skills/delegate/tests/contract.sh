#!/usr/bin/env bash
# Offline contract test: no provider CLI is really needed for dry-run argv
# checks; one fake `claude` runner exercises the full lifecycle end to end.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DELEGATE="$REPO/bin/delegate"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/delegate-contract.XXXXXXXX")"
trap 'rm -rf "$TMP"' EXIT
export XDG_STATE_HOME="$TMP/state"   # never touch the operator's job history
unset GROK_AGENT GROK_SESSION_ID
# Dry-run argv stays host-independent. A sandboxed parent would otherwise
# turn grok --read-only into --sandbox off.
export DELEGATE_GROK_PROFILE_APPLY=yes
fails=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }

REPO_GIT="$TMP/repo"; mkdir -p "$REPO_GIT"; ( cd "$REPO_GIT" && git init -q && git config user.email t@t && git config user.name t && echo s>s.txt && git add -A && git commit -qm init )

argv_of() { # provider -> space-joined argv from a dry-run
  "$DELEGATE" run --provider "$1" --cwd "$REPO_GIT" --model M --effort high --max-turns 9 --dry-run -- "task body" 2>/dev/null \
    | python3 -c "import sys,json;print(' '.join(json.load(sys.stdin)['providerExtra']['argv']))"
}

check() { # name provider needle...
  local name="$1" prov="$2"; shift 2
  local a; a="$(argv_of "$prov")"
  local ok=1
  for needle in "$@"; do case "$a" in *"$needle"*) ;; *) ok=0; echo "   missing: $needle" ;; esac; done
  [ "$ok" = 1 ] && pass "$name posture" || fail "$name posture"
}

check "grok"   grok   "grok" "--permission-mode auto" "--no-plan" "--no-subagents" "--sandbox workspace" "--trust" "--single"
check "claude" claude "claude -p" "--permission-mode auto" "--add-dir" "--output-format stream-json"
check "codex"  codex  "codex exec" "--approve-for-me" "--json"
check "cursor" cursor "cursor-agent --print" "--force" "--trust"
check "pi"     pi     "pi -p" "--no-session" "--no-extensions" "--mode json"

# codex must NOT carry --sandbox (mutually exclusive with --approve-for-me)
case "$(argv_of codex)" in *"--sandbox"*) fail "codex omits --sandbox";; *) pass "codex omits --sandbox";; esac

# --read-only maps to each runner's no-write posture; providers without one are refused
ro_argv() { "$DELEGATE" run --provider "$1" --cwd "$REPO_GIT" --read-only --dry-run -- "task body" 2>/dev/null \
  | python3 -c "import sys,json;print(' '.join(json.load(sys.stdin)['providerExtra']['argv']))"; }
ro_check() { # name provider needle...
  local name="$1" prov="$2"; shift 2
  local a; a="$(ro_argv "$prov")"; local ok=1
  for needle in "$@"; do case "$a" in *"$needle"*) ;; *) ok=0; echo "   missing: $needle" ;; esac; done
  [ "$ok" = 1 ] && pass "$name read-only posture" || fail "$name read-only posture"
}
ro_check "grok"   grok   "--sandbox read-only" "--trust" "--disable-web-search"
# Parent sandbox refuses the read-only profile: the run still launches.
DELEGATE_GROK_PROFILE_APPLY=no "$DELEGATE" run --provider grok --cwd "$REPO_GIT" --read-only --dry-run -- x >"$TMP/ro-inherit.out" 2>"$TMP/ro-inherit.err"
rha="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["providerExtra"]["argv"]))' "$TMP/ro-inherit.out")"
case "$rha" in *"--sandbox off"*--trust*--disable-web-search*) pass "read-only inherits when profile blocked";; *) echo "   argv: $rha"; fail "read-only inherits when profile blocked";; esac
grep -q "post-run delta check" "$TMP/ro-inherit.err" && pass "read-only inherit warns" || fail "read-only inherit warns"
ro_check "claude" claude "--permission-mode plan" "--disallowed-tools Edit,Write,NotebookEdit,WebSearch,WebFetch"
ro_check "codex"  codex  "--sandbox read-only" 'approval_policy="never"'
ro_check "cursor" cursor "--mode plan" "--sandbox enabled"
"$DELEGATE" run --provider pi --cwd "$REPO_GIT" --read-only --dry-run -- x >/dev/null 2>&1
[ $? -eq 64 ] && pass "pi --read-only refused" || fail "pi --read-only refused"

# usage errors -> 64
"$DELEGATE" run --provider grok --cwd "$REPO_GIT" >/dev/null 2>&1; [ $? -eq 64 ] && pass "no-prompt exit 64" || fail "no-prompt exit 64"
"$DELEGATE" run --provider grok --cwd "$REPO_GIT" --task /nope -- x >/dev/null 2>&1; [ $? -eq 64 ] && pass "bad-task exit 64" || fail "bad-task exit 64"

# dash-prefixed prompt survives after --
a="$("$DELEGATE" run --provider claude --cwd "$REPO_GIT" --dry-run -- "-x dashy" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)['providerExtra']['argv'][-1])")"
case "$a" in *"-x dashy"*) pass "dash-prompt preserved";; *) fail "dash-prompt preserved";; esac

# Full lifecycle with a fake claude runner (no trust gate).
FAKEBIN="$TMP/bin"; mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/claude" <<'FAKE'
#!/usr/bin/env bash
# Emit a minimal stream-json: one tool_use then a result. Actually write the file.
cwd="$(pwd)"
printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Write","input":{"file_path":"proof.txt"}}]}}'
echo WRITTEN > "$cwd/proof.txt"
printf '%s\n' '{"type":"result","subtype":"success","num_turns":1,"total_cost_usd":0.01}'
exit 0
FAKE
chmod +x "$FAKEBIN/claude"
env PATH="$FAKEBIN:$PATH" "$DELEGATE" run --provider claude --cwd "$REPO_GIT" --scope . -- "make proof" >"$TMP/env.json" 2>/dev/null
rc=$?
python3 - "$TMP/env.json" "$rc" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); rc=int(sys.argv[2])
ok = (rc==0 and d["exitCode"]==0 and d["stopReason"]=="end_turn"
      and d["emptyDiff"] is False and d["turns"]==1 and abs((d["costUsd"] or 0)-0.01)<1e-6)
print("PASS  fake-runner lifecycle" if ok else f"FAIL  fake-runner lifecycle: {d}")
sys.exit(0 if ok else 1)
PY
[ $? -eq 0 ] || fails=$((fails+1))

echo
if [ "$fails" -eq 0 ]; then echo "contract.sh: ALL PASS"; else echo "contract.sh: $fails FAILED"; fi
exit $([ "$fails" -eq 0 ] && echo 0 || echo 1)
