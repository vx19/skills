# Report

Load during SKILL.md phase 6. This is the user-facing output. Fill every
section. Use PASS / FAIL / NOT AVAILABLE for validation. Audit mode runs
all checks and reports what prepare mode would change, but makes no
edits. The verdict is **not ready** when an unresolved concern can
expose a secret or unintended private data, make the first push fail,
mislead users, or leave the repository unverifiable. Normal public
contributor identity is not a concern by itself.

## Template

### Verdict

Exactly one: **READY** or **NOT READY**, followed by one sentence naming the
decisive evidence. READY means no blocking concern was found in the reviewed
scope; never say "verified free of PII". Non-blocking observations do not force
**NOT READY**; label them separately from publication blockers.

### Repository assessment

3-5 sentences. What this repo is, its shape and entry points, and the
state you found it in — separate from what you changed.

### Publication scope

Name the exact intended public artifact: the full repo, a subtree, or an
export destination. Summarize the include/exclude decisions by file group
with reasons. State clearly when excluded development material remains in
the local source repo. If publishing the whole repo, say why its complete
tree is intended for public use.

### Pre-existing vs introduced

Pre-existing defects you found but did not cause, versus breakage you
introduced and fixed. Never blur the two.

### Changes made

Group by:

- security/privacy
- code cleanup
- documentation
- architecture
- repository structure
- development tooling

In audit mode, rename this section **Recommended changes** and describe
the same concrete actions without implying they were applied.

### Files removed

List every removed path and why removal was safe. Label provable slop
separately from obsolete code or documentation; slop evidence may be
generated output, empty scaffolding, an abandoned scratch file, a stale
duplicate, or an unreferenced temporary artifact. Also include small
slop cleanups made inside retained files. If none, say none. In audit
mode, call this **Files to remove** and describe the proposed action.

### Files added

New docs and example config. If none, say none.

### Remaining concerns

Only items that still need a human or cannot be proved safe to change.
Include ambiguous dead code, unclear ownership or policy, conflicting
docs with no demonstrable authority, unstated or contradictory
licensing, unpublished dependencies, and binaries or images you could
not inspect. A deliberately stated no-license/all-rights-reserved posture
is not automatically a blocker. Do not put provable slop here merely
because removing it felt cautious.

Split this section into **Blocking** and **Non-blocking**. A concern blocks
publication only when it can expose secrets or unintended private data, make
the push fail, materially mislead users, violate a required policy, or leave a
material part of the repository unverifiable.

### Potential secrets requiring manual action

Working-tree hits you replaced, plus anything that may still live in
git history or in an external service. History hits need rotation at
the issuer even if HEAD is clean. Report only the finding category,
safe path and line or blob/object identifier, exposure location, and required
action. Never print a raw secret or a full matching line that may
contain one.

### History / identity exposure

List identity only when there is evidence that it is unintended private
data: for example a private address, local-machine account, internal
domain, or identity the user asked not to publish. Preserve and do not
flag ordinary public author/committer identity, AUTHORS entries,
copyright notices, citations, or contributor credit merely because it
contains a name or email. List history-secret findings here by category
and blob/object ID only; never print the secret. History rewrites require the
user to name the tool and refs this turn; report the exact approval and effect
if one occurred.

### Historical large objects / LFS

Report oversized reachable blobs by object ID, safe path, and size, plus
malformed or missing Git LFS tracking that could make the
first GitHub push fail. Distinguish an actual push risk from a large but
acceptable tracked asset. Recommend remediation, but do not rewrite
history automatically.

### Validation results

| Check | Result |
|-------|--------|
| tests | PASS / FAIL / NOT AVAILABLE |
| lint | PASS / FAIL / NOT AVAILABLE |
| formatting | PASS / FAIL / NOT AVAILABLE |
| type checks | PASS / FAIL / NOT AVAILABLE |
| build | PASS / FAIL / NOT AVAILABLE |

A FAIL that you did not introduce is still FAIL, with a note. Report the
`publish_gate.py` JSON `gate` (`PATTERN_CLEAN` / `REVIEW` / `BLOCKED`, or NOT
AVAILABLE with reason) next to the contextual-review result (PASS / FAIL /
NOT AVAILABLE). A gate result alone never establishes readiness. Include the
exact artifact commit/tree, scanner exits/completeness, supplied source coverage,
fresh-clone profile/command results, and watched metadata outcome when used.

### Not checked

Required even when scans pass. State that no bundled scan exhaustively covers:

- bare names, postal addresses, or phones without `+` reliably
- image pixels/OCR
- source repo reflogs, stashes and dangling objects
- host surfaces: issues, wiki, releases, PR text, forks, cached force-pushed commits

Record any separate manual review and evidence, plus other unavailable checks
or private sources not supplied. Snapshot clones exclude the source's local
Git stores; host copies may still exist. These are scope limits, not proof that
such surfaces are clean. Material unverified exposure is a blocker.

### Keeping in sync

Required when a snapshot is used. State the chosen replace/chain style and
include the workflow from [snapshot-and-sharing.md](snapshot-and-sharing.md):
one local trunk, one-way snapshot publication, host changes returned as patches
to the trunk, and private-history backup off the host. Record the backup's
confirmed existence without private path details. Do not merge the source and
snapshot histories. Name the next exact command requiring user approval.

### Suggested commit

Do not commit unless asked. Push only with the user's named approval this
turn, from the isolated snapshot clone; record any approved action.

Title, conventional style, for example:

`chore: prepare repository for public release`

Body: the important cleanup, not a file dump.

### GitHub readiness

- [ ] Intended public boundary defined; tracked and exportable file groups have include/exclude decisions
- [ ] Docs, ADRs, issues, development notes, and config examples reviewed for user value and provenance
- [ ] Exact tree intended for publication matches the approved inventory; inspect the staged index when staging is part of the publication path
- [ ] Prior public history checked for excluded paths that remain reachable, or confirmed that no public destination history exists
- [ ] Separate PII scan and contextual review completed against approved public identities; unnecessary names and identity links removed or flagged
- [ ] License/author fields reviewed; user-owned attribution uses the approved identity and required third-party notices remain
- [ ] Secrets and unintended private data gone from the public tree (and history flagged)
- [ ] Git history inspected; secrets and unintended identity exposure flagged
- [ ] Historical object sizes and Git LFS usage checked
- [ ] Binaries and images inspected or listed as Remaining concerns
- [ ] `.gitignore` covers env, caches, build output, local state
- [ ] README matches the implementation
- [ ] Architecture matches the implementation (or N/A)
- [ ] Included examples are necessary to public setup/use, fake, and current; optional examples omitted when not needed
- [ ] Canonical install / run / test commands work
- [ ] Project checks run (or honestly NOT AVAILABLE)
- [ ] License stated or explicitly unset
- [ ] remote hash verified with `git ls-remote`
- [ ] history replaced or chained (style chosen)
- [ ] backup of the private history exists off the host
- [ ] publisher identity verified on every published commit
- [ ] bot access verified (role, token scope, instructions)
- [ ] Snapshot/push actions performed only with named approval this turn, or none performed
- [ ] Provable slop removed (or listed in audit mode)
- [ ] Ambiguous concerns listed, none silently dropped

Tick a box only with evidence. Mark conditional items N/A with a reason (for
example, no bot sharing, snapshot not used, or no push requested yet). Remote
hash verification stays pending until an authorized push/check; a local READY
verdict is not a claim of completed publication. If a required applicable item
is unchecked, the verdict is **NOT READY**.
