---
name: github-ready
description: Prepare a local repository for GitHub publication, public or private and shared with a bot. Scanners find candidates; the agent reads the tree, reviews privacy and provenance, fixes proven issues, aligns docs, runs available checks, and reports readiness. A snapshot or push happens only with the user's named approval this turn. Use when the user says clean up my repo before I push it to GitHub, clean up before push, prepare this repo for GitHub, make this repo public, publish a snapshot, share this repo with a bot, hand a repo to a bot or AI reviewer, public release prep, sanitize the repo, GitHub readiness, /github-ready, or /repo-publish-prep. Not for routine code-quality cleanup, secret-only scans, or architecture study.
metadata:
  version: 2.0.0
  argument-hint: "[path] [audit]"
license: MIT
---

# github-ready

Make a local repository safe and understandable to publish. Inspect the
whole tree. Fix what you can prove. Flag the rest. Snapshot and push actions
require the user's named approval this turn.

## When to use / when NOT to use

**Use** when the user wants this repo (or a named path) ready for a
public or private GitHub remote or sharing with a bot: first publish,
"is this safe to push", strip secrets before sharing, "clean up my repo before I push it to GitHub",
"get this repo ready for GitHub", `/github-ready`, or `/repo-publish-prep`.

**Do NOT use** for:

- Routine code-quality cleanup (`cleanup-code` and similar)
- Secret-only or "is this clone safe to run" (`local-security-audit`)
- Architecture study (`repo-analyzer`)
- Instruction-file drift (`drift-audit`)
- Standalone GitHub administration or pushing without a preparation review

## Modes

| Signal | Mode | Mutates |
|--------|------|---------|
| default, "prepare", "make public", "clean up before push" | **prepare** | working tree, after phase 0 and the audit list exist |
| "audit only", "don't change files", `audit` | **audit** | none; report only |

Arguments: first token, when present, is an existing directory; when
omitted, the target is the git root of the current workspace. `audit`
anywhere selects report-only. Nothing else is an argument. Do not
fetch, clone, or switch branches for discovery. An approved snapshot and its
isolated local validation clones are exceptions; see phase 5.

Both modes run every phase. In **audit**, inspect and report instead of
editing; run a check only when it cannot change the target or run it in
an isolated temporary copy. Otherwise mark it NOT AVAILABLE. In
**prepare**, make proven, scoped fixes without waiting for confirmation.

## Workflow

Run in order. Load a reference only when that phase starts.

### 0. Pre-flight (read-only, before any mutation)

Resolve `scripts/safe_repo_scan.py` relative to this `SKILL.md` and run
`python3 <scanner-path> TARGET --diff-safety` before any other listing.
Use its redacted change inventory to note the branch plus staged,
unstaged, and untracked changes; do not print raw `git status` paths.
Every pre-existing user change is protected: never revert, reset,
checkout, or clean it, and never sweep it into your commit. Work on
separate paths so your edits stay distinguishable from theirs. If a
needed fix overlaps user work, list it under Remaining concerns instead
of touching it. This protection also covers pre-existing untracked
debris: ignore it when appropriate, but do not delete it from disk.

### 1. Audit (read-only)

Map what the project is, from disk:

- purpose and entry points
- layout, config, scripts, generated files
- docs, tests, runtime/deploy assumptions
- dependency manifests
- personal settings hard-coded as constants: plan dates, savings or retirement
  targets, bundle identifiers carrying the owner's name, personal thresholds.
  Recommend user configuration that fails clearly when unset, plus a structural
  guard test rejecting hard-coded dates/targets in application code. Runtime
  changes remain proposals unless separately authorized.

Define the intended public artifact: this whole repository, a subtree, or a
curated export into another repository. Tracked does not mean approved for
publication. Inventory file groups and mark each **publish**, **keep local**,
**remove from source**, or **needs review**, with a brief reason. Read
[references/publication-scope.md](references/publication-scope.md) during this
phase; it covers docs, examples, provenance, and verification of exported
snapshots.

Keep an internal issue list. Do not delete or rewrite yet.

### 2. Safety

Load `references/checklist.md` (Secrets, Privacy, Git hygiene).

Scan filenames and contents, including git-relevant files, docs,
examples, configs, scripts, notebooks, logs, fixtures, tests, comments,
and sample data. Look for encoded or less-obvious secrets, not only
variable names that say `KEY`.

Run the separate PII candidate scan and contextual review described in
[references/pii-review.md](references/pii-review.md). Establish the publisher's
approved public identity and flag unnecessary real names, obsolete handles,
and connections to private identities, including license notices. A clean
secret scan does not establish PII safety.

When the project handles the user's private records, also run
`scripts/value_overlap_scan.py` against them (`pii-review.md`, "Values copied
from private data"). Real amounts, rates, references and plan dates pasted
into examples and tests pass every secret and identity scan.

If `local-security-audit` is installed in this session, run its scanner
on the target and treat those findings as inputs. Do not skip the
checklist pass. Do not install scanners or fetch tooling; there is no
network this pass.

Binary and image files you cannot read (pixels, packed archives): list
them for a human. Never claim you reviewed a file you could not open.

Replace live credentials with obvious placeholders (`YOUR_API_KEY`) in
files classified for publication. Keep local-only material out of a
curated artifact instead of rewriting its development history for release;
report any real credential found and recommend rotation. Document required
environment variables in the README or an optional `.env.example`, whichever
better serves public setup. Do not add an `.env.example` just because the
project uses env vars. Ignore `.env` and other secret files in `.gitignore`.

Replace accidental private or confidential data in publication-classified
project content and fictional/sample records with neutral examples. Keep
local-only development records out of a curated artifact rather than
rewriting them solely for release. Preserve licenses, copyright notices,
AUTHORS files, contributor credits, changelogs, citations, third-party
notices, and git identities unless the user explicitly asks to alter that
attribution.

Do not print a raw secret. Redact in chat, in diffs you quote, and in
the report.

### 3. Substance

Stay in `references/checklist.md` (Dead code, Consistency, Dependencies,
Scripts).

Remove only what you can show is unused or obsolete. If usage is
ambiguous, list it under Remaining concerns. Do not perform a large
restructure unless the current layout is actually blocking a new
developer.

In **prepare**, remove obvious slop automatically: OS metadata, editor
swap/backup files, caches, and generated build output that is proven
reproducible, not a release input, and already excluded by project
convention (or should plainly be ignored). Update narrow ignore rules
when needed. Also remove empty or duplicate scaffolding only when the
tree proves it has no purpose. Authored notes, fixtures, examples,
assets, vendored files, and anything with uncertain ownership or use are
not slop. That does not make them automatic publication inputs: keep local
source material unless deletion is proven safe, and omit it from a curated
public snapshot when it is not needed by public users. Record the distinction
between source cleanup and publication exclusion. These cleanups must not
change an API, schema, runtime behavior, or supported workflow.

Prefer one canonical command per common operation (install, run, test,
lint, format, build). Mark or remove scripts that no longer match the
tree.

### 4. Docs

Load `references/docs.md` and apply the publication-scope decisions from
phase 1.

Read every project doc, not only `README.md`, to find stale claims and
private development context. Reading a file is not a decision to publish it.
Classify ADRs, issues, backlogs, context notes, changelogs, agent instructions,
and setup docs by whether an external user or contributor needs them. Keep
useful local history in the source repo while excluding it from a curated
public artifact when appropriate.

Fix root `README.md` in place from the implementation; create one only
if it is missing. Never replace a working README with the template.
Keep architecture detail in the README; add `docs/architecture.md` only
when the README cannot hold it, with a Mermaid flowchart of what exists.

Review each example independently. Publish only examples that materially
help supported setup, usage, or tests; make included examples fake, current,
and runnable where that is cheap. A neutral default config may be enough.
`.env.example` is optional, not a publication requirement. If excluded
examples are referenced by docs, runtime errors, or tests, update those
references or use generic test fixtures.

### 5. Validate

Run the project's own checks, constrained:

- Never run tests that need live credentials, billing accounts, or
  production access.
- Never start servers you cannot stop, migrate databases, or touch
  external services.
- Never network-install (`npm i`, `curl | sh`) to make checks pass.
- Never run a repo-wide formatter unless the repo already formats on CI.

A check you cannot run safely is NOT AVAILABLE, not PASS. Fix breakage you
introduced; label pre-existing failures. Do not disable tests to go green.

Review the complete change set: no accidental deletions, unrelated
reformat, generated junk, user files swept in, secrets, or private paths.
Start with the scanner's redacted status and size summaries. Never print an ordinary diff
for a file that contained a suspected secret or unintended private data.
Resolve scripts relative to this `SKILL.md`. Keep phase 0's `--diff-safety`
for change inventory, but run both final scans:

```bash
python3 <skill>/scripts/safe_repo_scan.py TARGET
python3 <skill>/scripts/pii_scan.py TARGET --history
```

Repository mode includes reachable history; diff mode is history-blind. PII
history scanning is opt-in. Apply the approved identity policy files from
`references/pii-review.md`. Inspect metadata-only findings and re-scan after
fixes. Review unaffected files normally; list unreadable binaries/images as
Remaining concerns.

Scanner exits: `safe_repo_scan.py`: 0 clean/complete, 1 suspected secrets,
2 incomplete (takes precedence); `summary.suspected_secrets` signals hits,
not large-object risk. `pii_scan.py`: 0 no candidates, 1 review candidates,
2 incomplete. `value_overlap_scan.py`: 0 no overlap, 1 review overlaps,
2 incomplete. None establishes absence of private data.

Scan the exact publication output and its staged index against the approved
inventory after edits; a clean source worktree does not prove an export clean.

If git history exists, flag secrets that may already be in old commits
and author/committer identities only when they expose unintended private
or internal information on first push. Ordinary public contributor
identity is expected and is not a concern by itself.
Inspect reachable history for oversized blobs and check that files meant
for Git LFS are represented by pointers and covered by attributes.
Report only object/blob IDs, safe paths, sizes, and categories; never print secret
contents. Removing secrets or large files from HEAD is not enough.
When source history must not be published, load
`references/snapshot-and-sharing.md` and propose `scripts/publish_snapshot.sh`.
Invoke the skill's copy with the source as the working directory, or copy it
into the target only on the user's named approval. It creates/updates
an approved snapshot branch and a persistent isolated clone; it never pushes.
The script runs `publish_gate.py` on that clone: 0 `PATTERN_CLEAN`, 1 `REVIEW`, 2 `BLOCKED`.
Record its JSON `gate` beside contextual review; the gate alone is not readiness.
Review candidates before proposing `--accept-review`; blocked gates cannot be
accepted. See the reference for identity, exclusions, gate policy and lease.

After an approved snapshot build, run `fresh_clone_check.py` on the snapshot
clone with `--watch` on the private data folder and the project's actual setup
and test commands via repeatable `--run`. Exit 0 means commands passed and
watched metadata was unchanged; 1 means a command failed; 2 means an error or
watched change. This is environment isolation, not a filesystem sandbox.
Include the one-way sync workflow in the report.

When a destination already has history, report omitted paths still reachable
from its target branch. A deletion commit does not remove prior content.
History rewrite stays forbidden unless the user names the tool and the refs
this turn. End this phase with the exact next command needing approval: the
snapshot invocation, or, after validation, the lease-protected push:
`git -C <out> push --force-with-lease=main:<expected-remote-hash> <remote-url> publish:main`.
For a first push use `--force-with-lease=main:` (empty expected value).
Push always from the isolated clone, never the source: the clone holds only
snapshot commits, so no ref typo can send private source history. Verify an
approved push with `git ls-remote`; do not run it without network authorization.

### 6. Report

Load `references/report.md`. Emit that report. Propose a conventional
commit. Do not commit unless the user asked. Snapshot/push actions require
the user's named approval this turn.

End with an explicit `READY` or `NOT READY` verdict. Do not claim ready
while a blocking concern, history secret, unintended identity exposure,
push failure, misleading public documentation, or material unverified
surface remains open. Clearly label non-blocking observations instead of
letting harmless uncertainty veto publication.

## Hard rules

- Protect pre-existing user work. Never revert, reset, checkout, or
  clean user files, and never mix your edits into their WIP.
- Never `git reset --hard` or `git clean -fd`.
- Never `git add -A` or `git commit --amend`. Stage only files you
  changed, and only when a commit was requested.
- History rewrite (`rebase`, `filter-repo`, BFG, `filter-branch`) stays
  forbidden unless the user names the tool and the refs this turn.
  "Prepare this for GitHub" never counts as that order.
- Never `git push`, add/remove a remote, or create a GitHub repo; never
  create, rename, or delete branches or tags. The one exception is an action
  the user approves by name this turn, such as copying/running
  `publish_snapshot.sh` for a stated snapshot branch or one stated push from
  its isolated clone; verify its effect after. Never push from the source.
- Never change an API, schema, or runtime behavior to "clean".
- Never print or commit a raw secret value.
- Never delete authored material or a symbol unless you can show it is
  unused and not a public, documented, or dynamic entry point. Phase 3's
  narrowly defined disposable artifacts may be removed in prepare mode;
  ambiguous items stay in Remaining concerns.
- Never invent components, commands, or env vars that are not in the tree.
- Never disable failing tests, bump majors "to look current", or
  reformat repo-wide (unless CI already formats).
- Never write skill scratch files (reports, notes, audit logs) into the
  target. The report is chat output only.
- Never claim GitHub-ready while material blocking uncertainties remain;
  label harmless, non-blocking observations without letting them veto release.
- No network (`git fetch`, curl, installer from remote) unless the user
  asked for it this turn. Checks that need network run NOT AVAILABLE.

## References

- `references/checklist.md`: what to search and what to ignore
- `references/docs.md`: README, architecture, examples
- `references/publication-scope.md`: publication boundary, provenance, and exact-output checks
- `references/report.md`: final report, commit message, readiness checklist
- `references/pii-review.md`: identities, PII candidates, private values, attribution
- `references/snapshot-and-sharing.md`: snapshot, sync, fresh clone, lease, bot access
- `scripts/pii_scan.py`: metadata-only PII candidates in files and optional local history
- `scripts/value_overlap_scan.py`: tracked files and lines that carry numbers from the user's private sources; never prints a value
- `scripts/safe_repo_scan.py`: metadata-only worktree/history/size/diff scan
- `scripts/publish_gate.py`: aggregate pattern, identity, commit-count and ref gate
- `scripts/publish_snapshot.sh`: approved replace-style snapshot and isolated clone
- `scripts/fresh_clone_check.py`: isolated environment checks and watched metadata
