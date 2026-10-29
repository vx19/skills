# PII and identity connections

Run this review separately from the secret scan, on both the source and the
exact artifact intended for publication. Decide the publisher's public identity
from the user's instructions. Do not assume an existing license holder, old
handle, Git author, email, or repository owner is the desired public identity.
Use an approved pseudonym for user-owned attribution when explicitly authorized.
Avoid unnecessary real names and identity connections in public project content.

## Candidate scan

Run the bundled, dependency-free scanner:

```bash
python3 <skill>/scripts/pii_scan.py TARGET --allow-file /tmp/public-identities.txt --deny-file /tmp/private-identities.txt
```

Policy files contain one exact identity per line; blank lines and `#` comments
are ignored. Keep these files outside both source and public repositories; do
not publish the denied values or print matched lines. An allow entry approves
only that exact candidate, not a file or every reference on the same line.
Denied values take precedence. Policy files are optional, but use a deny file
for known private names, obsolete handles, private emails, and aliases supplied
by the user. Never hardcode a particular person's identities into this skill.

For a Git destination also run with `--history`. It checks local reachable
file blobs, author/committer identities, and commit messages. It does not fetch
remote refs. Report incomplete remote visibility rather than claiming a remote
history was scrubbed. Exit 0 means no heuristic candidates, 1 means candidates
need review, and 2 means incomplete scan. Output contains categories, safe paths,
line numbers, or object IDs; no matched values. Candidate-shaped paths are
redacted. Categories are:

- `email`, `home-identity`, `github-account-link`
- `copyright-holder`, `author-field` (quoted fields and limited unquoted forms)
- `git-ssh-remote` (SCP-style Git remote owner), `profile-link` (limited account
  prefixes for GitLab, Bitbucket, Codeberg, X/Twitter, LinkedIn and Medium)
- `image-metadata` (Exif/XMP marker only), `phone-candidate` (leading `+` with
  the supported separator/digit shape)
- `denied-identity` (policy match), `filename-identity-candidate`

Bare names, postal addresses, phones without `+`, unsupported profile forms,
and identities outside these patterns may be missed. Exif/XMP markers are not
metadata decoding; image pixels/OCR are not scanned. Binaries get byte-pattern
scans; symlinks are not followed and require separate review when included.
No mode inventories source reflogs, the full stash stack or dangling objects;
objects reachable through a current stash ref may still be scanned. Host
issues, wiki, releases, PR text, forks and cached force-pushed commits are
outside these scans.

## Values copied from private data

When the repository works on the user's private data (finances, health,
customers), ask where that data lives and run the bundled overlap scan against
it. Keep the sources outside the target; the scan refuses a source inside it.

```bash
python3 <skill>/scripts/value_overlap_scan.py TARGET \
  --source /path/to/private/records.json --source /path/to/private/records.csv \
  --csv-column quantity --csv-column amount \
  --values-file /path/to/private/values.txt
```

It reads numbers from JSON values and strings, chosen CSV columns, or any text
file, and reports each tracked file and line that carries a distinctive one.
Output never contains a value. Exit 0 means no overlap, 1 means review every
location, 2 means an incomplete scan. Supply at least one `--source` or
`--values-file`; both repeat. A values file holds one number per line, ignoring
blank lines and `#` comments. Convert scaled-integer stores (amounts x100,
quantities x10^8) to plain values in the units used by the application first,
then pass that external file with `--values-file`; no scaling is automatic.

Defaults are `--min-sig 4` and `--min-int 10000`. The heuristic skips round
integers, some date-like numbers, repeated/ascending digits and long identifiers.
It scans tracked worktree files only, not history or untracked copies. Review
each finding in context:

- **Copied:** a rate, amount, fee or reference that only the user's records
  hold. Replace it.
- **Public:** a market price, an exchange rate from a public feed, or an amount
  any customer of the same program would pay (face value times a published
  ratio). Leave public prices out of the sources with `--csv-column`; move a
  test to an invented ratio when its amounts still collide.
- **Coincidence:** a synthetic value that happens to match. Change the test
  value; that is cheaper than an exception list, which would itself have to
  store the real value.

A clean overlap scan covers only the sources given. Transaction-level data the
user cannot or will not point the scan at stays a judgment call; say so in the
report.

## Required contextual review

Regex cannot reliably identify every real name or connection. A zero-hit scan
is not a privacy verdict. Inspect LICENSE/COPYING, AUTHORS, package author and
maintainer fields, contact URLs, README credits, docs, examples, fixture records,
source comments, generated output, filenames, image metadata, and CI/release
metadata. Look for real names, obsolete handles, private emails, home paths,
private projects, personal websites, and links that connect the approved
pseudonym to an earlier account or identity. A public URL is not automatically
necessary. Use neutral samples such as `Example User` and `user@example.com`;
do not invent realistic full names merely to replace personal data.

Classify every candidate: approved project identity, required third-party
attribution/citation, generic placeholder, removable personal context, or
unresolved. Record category/location and decision without echoing private
values. Required legal notices and third-party credits remain intact; do not
silently remove others' attribution to satisfy the publisher's pseudonym policy.
If attribution and privacy cannot both be satisfied, flag the conflict and
exclude an optional component when feasible. Authorized changes to the
publisher's own attribution preserve the license text and year.

Remove or neutralize unnecessary identity references in publication-classified
content. Keep local-only records outside curated exports. Re-scan the exact
public artifact after edits; verify that docs and runtime references still work.
Check prior public history separately: a deletion or replacement in the current
tree does not erase earlier content. Rewrite/push actions require the user's
named tool/refs or exact push approval this turn, as specified in SKILL.md.
Unresolved unintended identity exposure prevents READY.

Run `publish_gate.py` inside the one-commit snapshot clone with the same policies
and private sources, for example:

```bash
python3 <skill>/scripts/publish_gate.py <out> --identity "Example User <user@example.com>" --allow-file /tmp/public-identities.txt --deny-file /tmp/private-identities.txt --values-file /tmp/private-values.txt
```

It checks commit count (default maximum 1), non-branch refs, exact author and
committer identities on every reachable commit, repository-mode secrets, PII
with history, and value overlap only when `--source` or `--values-file` is given.
`--identity` repeats for approved identities; `--max-commits` changes the limit
for an explicitly chosen manual chain. Exit 0 / JSON `gate: PATTERN_CLEAN` means
no patterns matched; 1 / `REVIEW` means PII or overlap candidates; 2 / `BLOCKED`
means a blocking check or incomplete scan. Large-object risk still needs review;
the secret scanner's clean exit is not a size clearance. Record the JSON gate
value beside the separate contextual-review result.

Running the gate inside the one-commit clone makes the source's older history
and untracked copies irrelevant to the published tree: they were not copied.
This does not clear the source repo, cover host surfaces, or replace reading
the exact published files. Do not add private/untracked material to the clone.
