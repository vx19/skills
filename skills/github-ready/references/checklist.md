# Checklist

Load during SKILL.md phases 2 and 3. Search the target tree, including
ignored-but-present files that would ship if `.gitignore` is wrong.

Do not expose raw secret values in the terminal, tool output, chat, diffs,
scratch files, or the report. A command that prints a matching line is not a
safe scanner command.

## Secrets

Filenames and contents. Encoded or odd-looking blobs count. Placeholders
look like `YOUR_API_KEY`, `changeme`, `not-a-real-secret`.

Look for:

- API keys, tokens, passwords, private keys, certificates
- authentication headers, database credentials, connection strings
- cloud credentials, OAuth secrets, webhook secrets
- `.env` contents, hardcoded credentials
- internal URLs that embed credentials
- secrets in tests, fixtures, notebooks, docs, logs, screenshots, CSV/JSON

Also grep for shapes, not only names:

- `AKIA[0-9A-Z]{16}` (AWS)
- `ghp_`, `github_pat_`, `glpat-`, `xoxb-`, `sk-`, `sk-ant-`
- `-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----`
- long base64 next to `api_key` / `secret` / `token` / `password`
- `postgres://`, `mongodb+srv://`, `redis://` with a password
- `Bearer ` followed by a non-placeholder token

Replace live values with placeholders in files retained for publication.
Provide `.env.example` only when it materially improves setup and is within
the intended public scope; it is optional. Do not add one to satisfy this
checklist. Put `.env`, `*.pem`, `*.p12`, `id_rsa`, and similar on
`.gitignore`.

Scan git history separately, but never use commands such as unfiltered
`git log -p`, `git show`, or ordinary `git grep` output: they can print the
credential itself. Safe scan output contains only:

- the commit or blob ID
- the path and, only when useful, the line number
- the finding category and a count
- a fixed placeholder such as `[REDACTED]`, never matched bytes, surrounding
  source lines, a reversible encoding, or a value-derived preview

Resolve `scripts/safe_repo_scan.py` relative to this skill's `SKILL.md` and
run `python3 <skill>/scripts/safe_repo_scan.py TARGET` for the baseline scan.
Exit 0 means clean and complete, 1 means suspected secrets, 2 means incomplete
(and takes precedence). `summary.suspected_secrets` is the secret-hit signal;
inspect large-object findings separately even on exit 0. Repository mode
checks the worktree and reachable history; `--diff-safety` is only the change
inventory and does not scan history. It emits metadata only;
it must never be replaced with an unfiltered content-printing command. An
already-installed scanner may supplement it only after confirming and
enabling redaction for that installed version. Filename-only searches such as
`git grep -l` are acceptable for extra bounded secret-shape patterns; do not
include a real secret as the search term or echo the command with shell
tracing. Treat filenames themselves as sensitive when they contain personal
or secret material and redact them too. Report history hits as manual action.
Do not install a scanner or run `git filter-repo` / BFG unless ordered.

## Privacy

Apply `pii-review.md` as a separate PII pass, including license holders, obsolete
handles, and links connecting approved project identities to personal accounts.
A secret-pattern scanner alone does not cover these exposures.

Replace with neutral examples (`Example User`, `user@example.com`,
`/home/<account>/app`) only when the value is clearly accidental private
or confidential data, a machine-local trace, or realistic personal/customer
data used in a sample or fixture:

- personal names, emails, phone numbers, addresses
- usernames, employee/account IDs, customer data
- company-confidential notes
- internal hostnames and RFC1918 addresses used as real infra
- machine-specific paths (`/Users/<name>/`, `/home/<name>/`, `C:\Users\`)
- private repository URLs
- personal test data, local file locations

Preserve legitimate public attribution and provenance. Do not anonymize or
remove names, handles, emails, organizations, or URLs in copyright notices,
licenses, AUTHORS/contributor records, acknowledgements, credits, citations,
academic references, changelog attribution, vendored notices, signed-off-by
trailers, or source provenance unless the user explicitly asks and has the
right to do so. Public project contact details are not automatically private.
When context is ambiguous, flag the exact location and reason instead of
rewriting it.

Also scan `git log` author/committer names and emails for unintended
private or internal identities that would go public on first push, such
as a private mailbox, local-machine account, or internal domain. Do not
flag an ordinary public name, project address, or privacy-preserving
`noreply` address merely because it identifies a contributor. Identity
changes require history rewriting: report them, do not rewrite.

Inspect commit-prep artifacts, generated reports, logs, screenshots,
notebooks, CSVs, JSON, and fixtures.

## Git hygiene

Review `.gitignore` and `.gitattributes`. Ignore generated and local
state. Do not ignore files needed to reproduce development.

Usually ignore:

- `.env`, `.env.local`, `.env.*.local`; inspect `.env.example` separately and
  publish it only when it is useful, generic, and within the intended scope
- `.DS_Store`, `Thumbs.db`
- `.idea/`
- `.vscode/` only when it holds personal settings; keep shared
  `extensions.json` / `settings.json` if the project relies on them
- `node_modules/`, Python venvs (`venv/`, `.venv/`, `env/`)
- `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`
- `dist/`, `build/`, coverage output, logs
- local databases (`*.sqlite`, `*.db`) unless they are fixtures
- temporary exports, editor swap files

Untrack (`git rm --cached`) only what is both generated or secret and
not part of the user's current uncommitted work. Never untrack a file
the user has staged or modified in this session. A secret committed in
an earlier commit is history — flag it, do not rewrite it.

Never `git add -A`. Stage only files you changed, only when asked to
commit.

Keep lockfiles, fixtures, and example configs when the runtime, packaging,
tests, or public setup actually require them. Review each example for current
support, user value, and private machine/provider provenance. Tests should use
small generic fixtures when their purpose is to test parsing or behavior, so
they do not require publishing a provider-specific setup example.

### Historical size and Git LFS

Inspect objects reachable from all local refs, not only files in `HEAD`. Use
git object metadata (`git rev-list --objects --all` with
`git cat-file --batch-check`, or an equivalent local read-only method) so the
scan reports object ID, size, type, and path without dumping blob contents.
For GitHub.com, flag blobs larger than 50 MiB as warnings and blobs larger
than 100 MiB as push blockers; files beyond the hard limit require Git LFS or
history remediation. These thresholds come from GitHub's documented file-size
limits. For another host, apply its documented limit; when none is available,
flag unusually large objects for review and state the threshold used. A blob
that exceeds the intended host's hard limit is a publication blocker even if
the path was later deleted.

For every revision containing Git LFS-managed paths or pointer-like text,
check without fetching that:

- `.gitattributes` maps the intended path to the LFS filter
- the committed value is a canonical pointer with the LFS specification URL,
  a `sha256` object ID of 64 hexadecimal characters, and a decimal size
- LFS-tracked paths do not contain the full large binary by mistake
- pointer-like files are not truncated, hand-edited, merge-conflicted, or
  committed outside the matching LFS rule
- the referenced local LFS object exists when it is expected to be present;
  missing local content is reported as unverified, never fetched

Report the affected ref/commit, object ID, path, size, and problem category.
Do not print blob contents, run `git lfs fetch`, migrate objects, delete refs,
or rewrite history. Remediation that changes old commits remains a proposed
manual action unless the user explicitly authorizes the exact history rewrite.

## Obvious cleanup slop

Remove obvious repository debris during prepare mode instead of merely
listing it. An item qualifies for automatic removal only when all of these are
true:

- it is clearly disposable: an OS/editor artifact, cache, debug dump, test or
  coverage residue, generated build output, rejected-patch file, backup/temp
  file, abandoned export, or stale generated duplicate
- it is reproducible or has no project value, and no manifest, import,
  command, test, documentation, packaging rule, CI job, or dynamic entry point
  refers to it
- it is not source, a fixture/golden file, migration, lockfile, vendored
  dependency, legal/attribution material, required published asset, or the
  sole copy of information that cannot be regenerated
- it was not staged, modified, or newly created by the user before this pass,
  and its removal does not overlap protected user work
- the deletion is small enough to verify directly in the final diff

Typical decisive removals include committed `.DS_Store`, editor swap/backup
files, `*.orig` / `*.rej`, cache directories, debug logs and crash dumps,
reproducible coverage/build residue, and an unreferenced byte-identical or
clearly older generated copy when the canonical output is known. Remove a
leftover debug statement only when it is unambiguously ad hoc, has no
documented logging purpose, and removing it does not change an interface or
expected output; otherwise treat it as code, not slop.

If any criterion is uncertain, keep the item and add a Remaining concern with
the path, evidence that it may be debris, and the fact that would make removal
safe. Add an ignore rule when the debris is expected to recur, but do not
ignore artifacts the repository intentionally publishes.

## Dead code

Inspect unused modules, functions, classes, imports, obsolete scripts,
duplicate utilities, abandoned experiments, commented-out blocks, old
implementations, unused config, unreachable code, redundant wrappers,
obsolete compatibility shims.

Remove only when no reference is found in the tree — including strings
that name it (CLI entry points, dynamic imports, plugin names) — tests
do not use it, and it is not a public, documented, or dynamic entry
point. Otherwise flag it; do not delete.

If unsure, Remaining concerns: path, why it looks dead, what would
prove it.

## Consistency

Watch for:

- duplicated implementations
- competing config systems
- mixed naming or directory conventions
- conflicting env var names
- inconsistent error handling or logging
- duplicated constants, magic values
- stale TODOs and feature flags
- scripts that name files or env vars that no longer exist

Consolidate only when the merge is obvious. Prefer the layout a new
developer would expect over a new abstraction.

For each `TODO` / `FIXME` / `HACK`: resolve it, turn it into real docs,
or leave it with a one-line reason in Remaining concerns.

## Dependencies

From the actual manifests (`package.json`, `pyproject.toml`,
`requirements*.txt`, `Cargo.toml`, `go.mod`, and so on):

- unused, duplicate, or undeclared imports
- dev tools marked as production deps
- docs that name packages the code no longer uses
- obsolete pins that the install path does not match

Fix the install docs so they reproduce the environment. Do not perform
risky major upgrades as part of this pass.

## Scripts and commands

Every executable and documented command:

- still works
- current paths, filenames, env vars
- no personal paths, no secrets
- no undocumented local setup

Remove or mark obsolete scripts. Document one canonical command for
install, run, test, lint, format, build, and deploy if those exist.

Also look for leftover debug `print` / `console.log`, large binaries,
committed archives, duplicate files, outdated screenshots.
