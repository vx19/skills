# Snapshot publication and sharing with a bot

Use this when source history must stay private or the published repository
will be shared with a bot. Snapshot creation and each push require the user's
named approval this turn. Audit mode proposes them without running them.

## Publish a one-commit snapshot

Propose the bundled script, run with the source repository as the working
directory. Replace placeholders with reviewed paths and the approved identity;
end phase 5 with the exact command needing approval:

```bash
bash <skill>/scripts/publish_snapshot.sh --identity "Example User <user@example.com>" --out <out> --exclude-file /tmp/publish-excludes.txt --remote-url <remote-url> -- --allow-file /tmp/public-identities.txt --values-file /tmp/private-values.txt
```

`--out` must be outside the source and its Git directory, not an ancestor.
Use a new directory: reruns replace only a clone marked as this source's prior
snapshot output. The script refuses tracked changes; untracked files are not
published. Commit desired changes separately only when authorized. Omit
`--exclude-file` or value inputs when inapplicable. Keep policies and private
sources outside the published tree. `--branch` defaults to `publish`.
Use the approved public identity; a GitHub noreply address is recommended.
List both the public name and that email in the `--allow-file`; otherwise the
gate reports the snapshot's own commit metadata as REVIEW.

What it does:

- Sets and verifies both author and committer to `--identity`.
- Uses HEAD's tree and committer timestamp, a fixed message and a parentless
  commit: identical inputs reproduce the hash.
- Applies `--exclude-file` through a temporary index: literal relative paths,
  one per line, blank lines and `#` comments ignored; directories remove their
  descendants. HEAD, the real index and worktree remain unchanged.
- Updates the snapshot branch, clones with `--no-local --no-tags
  --single-branch`, removes origin, and runs `publish_gate.py` on that clone.
  Arguments after `--` go to the gate; see `pii-review.md` for policies.
- Keeps a successful isolated clone at `--out`; on failure it attempts to
  restore the previous snapshot ref and removes the newly built output.
- Prints hashes, counts, check summaries and a clone-based push with a lease;
  it does not push or verify the remote itself.

Exit 0 means a clone was retained after gate acceptance; explicit refusals
(including unaccepted review) exit 1; invalid CLI usage exits 2. Other tool
failures may propagate their exit status. Gate exits
are 0 `PATTERN_CLEAN`, 1 `REVIEW`, 2 `BLOCKED`; contextual review is always
required. Only after reviewing candidates may the user approve a rerun with
`--accept-review`; a blocked gate is never accepted. Capture the gate JSON
separately for the report using `publish_gate.py <out> --identity ...` with the
same policy/value arguments; the snapshot script prints summaries, not JSON.

Copy `publish_snapshot.sh` into the target only with the user's named approval.
When copied alone, set `PUBLISH_GATE` to the skill's `scripts/publish_gate.py`
(which needs its sibling scanners), or run the script directly from the skill.
Do not copy skill reports or private policy files into the target.

Always push from the isolated clone: it holds only snapshot commits, so a ref
typo cannot send private source history. The printed lease uses the previous
**local snapshot** hash; verify it equals the expected destination tip before
proposing the exact push. If the destination differs, stop and review it.
For an existing `main`:

```bash
git -C <out> push --force-with-lease=main:<expected-remote-hash> <remote-url> publish:main
git ls-remote <remote-url> refs/heads/main
```

For a first push, require that `main` does not exist (empty expected value):

```bash
git -C <out> push --force-with-lease=main: <remote-url> publish:main
```

Use the chosen branch in place of `publish` if `--branch` was supplied. Network
commands run only when the user requests them or approves the named action
this turn. After an approved push, verify the remote hash equals the snapshot
hash. Rebuild after every source commit.

Force-pushes do not erase cached commits, forks or existing clones. Report any
prior exposure; the host may need to purge cached objects. If sharing the
source `.git` itself, no scan inventories its reflogs, full stash stack or
dangling objects (a current stash ref may make some objects reachable). The
snapshot clone does not carry the source's reflogs, stashes or dangling objects.

## Keep the private history and the public snapshot in sync

After the first snapshot there are two unrelated histories: the full one on
the user's machine and the snapshot on the host. Explain this to the user and
leave them with one workflow:

- **One trunk on the machine.** All work happens there, on one trunk branch
  (often `main` or `master`) or short branches merged into it. Point out stale
  branches and worktrees that hold newer work than the trunk; a fast-forward
  (`git merge --ff-only <branch>`) is the safe way to catch the trunk up.
- **Publishing is one-way.** Commit locally, rebuild the snapshot, push it, and
  confirm `git ls-remote` prints the hash the snapshot script printed. That
  equality is the sync check.
- **Changes from the host come back as patches.** A bot or collaborator branch
  on the host is based on the reviewed snapshot. With network authorization,
  fetch the needed snapshot-based commits and review/cherry-pick them onto the
  trunk, accounting for any excluded files; then republish. Never merge the host branch into the trunk: that joins the
  histories, and the next push carries the private history out.
- **Back up the private history off the host.** It now exists on one machine
  only. Suggest `git bundle create <backup>/<repo>.bundle --all` on a disk or
  encrypted volume the user controls, or a machine backup that covers the
  checkout. Do not push it to a second hosted repository, private or not; it
  still holds whatever the snapshot was built to leave out.

Choose the style explicitly; the bundled script implements **replace only**.

| Style | Method | Tradeoff |
|---|---|---|
| Replace | Parentless snapshot, push with a lease | Old snapshots leave branch history; open PRs may go stale |
| Chain | Manual `git commit-tree <tree> -p <previous-published-snapshot>` | Release history and PR continuity; earlier snapshot content stays reachable |

Chaining requires a separately reviewed manual workflow, only snapshot parents,
and a gate commit limit chosen with `--max-commits`. Never chain to a source
commit. A passed pattern gate still requires contextual review.

## Prove the snapshot works from a fresh clone

Use the bundled checker on the successful snapshot clone. Supply the project's
actual local setup/tests; this example is for a Python unittest project:

```bash
python3 <skill>/scripts/fresh_clone_check.py <out> --run 'python3 -m unittest discover' --watch /path/to/private-data --profile local
```

`--run` repeats and runs sequentially via `/bin/sh -c`, stopping on failure.
The checker makes a fresh `--no-local --single-branch` clone, removes origin,
uses an empty environment with temporary HOME/TMPDIR, minimal PATH and Git
config isolation, and sets `GIT_CEILING_DIRECTORIES`. `--path` repeats to append
absolute tool directories; `--profile` is a report label, not a setup selector.
`--keep` retains the temporary directory; otherwise it is deleted.

`--watch` repeats for private directories outside the input repository. It
compares relative paths, sizes, mtimes and modes before/after, not file contents.
It does not follow symlinks and does not see reads: a write through a symlink
or a read of a watched file leaves the fingerprint unchanged.
Exit 0: commands passed and watched metadata unchanged; 1: command failure;
2: error or watched change. JSON records the commit, profile, supplied commands,
exit codes and watch counts; child stdout/stderr is suppressed. Keep commands
free of private literals. This is environment isolation, not a filesystem or
network sandbox: inspect commands first. It cannot prove data was never read
or that same-size content with restored metadata was unchanged. Report which
profile/checks ran and which were unavailable.

## Private values that scans miss

Secret and identity scans do not catch a real amount, rate, quantity, account
reference or plan date copied into an example, fixture or test. These turned
up only after a clean secret scan:

- an example string in a prompt or docstring built from a real record;
- a test that rebuilds one real event from its date, amount and rate;
- fixture rates and fees copied from a real ledger;
- a real-looking institution code (bank branch, routing code) in a parser
  test, where a synthetic code fitting the same pattern works;
- a personal plan date or target held as a code constant;
- the owner's name in an application bundle identifier;
- a home-directory path (`/Users/<name>/...`) in a test URL.

Run `scripts/value_overlap_scan.py` with the user's private sources (see
`pii-review.md`). Replace each confirmed value with a format placeholder
(`EUR<amount>@<rate>`) in prose, and with an invented value that keeps the
test's arithmetic in code. Move personal settings to the user's configuration
and fail clearly when unset. When the target repository has a pre-commit hook,
recommend adding the same overlap check there, reading the private sources at
scan time and reporting path and line only, so the next copy is refused at
commit.

## Share with a bot

Keep the repository private and verify access under the bot's own identity:

1. Verify the collaborator permission is `read`:
   `gh api repos/OWNER/REPO/collaborators/BOT/permission`.
   This needs network; run it only when the user asks, or have the user run it
   and record their confirmation. Do not infer the role from an invitation.
2. A third party cannot inspect a fine-grained token's selected scope. The
   user confirms on GitHub's token settings page that it covers exactly one
   repository with Contents read-only. Record who confirmed it (an approved
   public identity or role), when, and what was confirmed; never record the
   token. If no token is used, mark this check not applicable with the reason.
3. Read the target's agent-instructions file: it must state what the bot may
   read, that it has no private data, and how to run setup/tests. Verify those
   commands against the published tree and fresh-clone results.

Do not provide an owner's token, a classic token, or write access. Private data
stays outside the repository. If a separate data channel is required, review
its allowlisted fields and read-only access separately; repository checks do
not verify it. Never connect the owner's main cloud account to the bot.

## Working with the user on publish actions

- The user approves a snapshot build or exactly one named push this turn.
  Prepare each exact command for review; run no push from the source repo.
- Give commands separately and check their effects: clone commit, gate JSON,
  fresh-clone result, and remote hash after an authorized push.
- Report safe locations, counts and hashes, never private values.
