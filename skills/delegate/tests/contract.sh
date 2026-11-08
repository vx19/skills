#!/usr/bin/env bash
# Offline smoke test of the `bin/delegate` entry point as a shell sees it: a
# dry run prints an envelope, usage errors exit 64, and one fake `claude`
# runner goes through the full lifecycle. Each provider's argv is checked in
# tests/test_argv.py.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DELEGATE="$REPO/bin/delegate"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/delegate-contract.XXXXXXXX")"
trap 'rm -rf "$TMP"' EXIT
export XDG_STATE_HOME="$TMP/state"   # never touch the operator's job history
unset GROK_AGENT GROK_SESSION_ID
fails=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }

REPO_GIT="$TMP/repo"; mkdir -p "$REPO_GIT"; ( cd "$REPO_GIT" && git init -q && git config user.email t@t && git config user.name t && echo s>s.txt && git add -A && git commit -qm init )

# A dry run prints one schema-3 envelope with the runner first in argv.
"$DELEGATE" run --provider claude --cwd "$REPO_GIT" --dry-run -- "task body" >"$TMP/dry.json" 2>/dev/null
python3 - "$TMP/dry.json" <<'PY' && pass "dry-run envelope" || fail "dry-run envelope"
import json,sys
d=json.load(open(sys.argv[1]))
sys.exit(0 if d["schemaVersion"]==3 and d["dryRun"] and d["providerExtra"]["argv"][0]=="claude" else 1)
PY

# usage errors -> 64
"$DELEGATE" run --provider grok --cwd "$REPO_GIT" >/dev/null 2>&1; [ $? -eq 64 ] && pass "no-prompt exit 64" || fail "no-prompt exit 64"
"$DELEGATE" run --provider grok --cwd "$REPO_GIT" --task /nope -- x >/dev/null 2>&1; [ $? -eq 64 ] && pass "bad-task exit 64" || fail "bad-task exit 64"

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
