# Publication scope and provenance

Use this during every public-release preparation. It is especially important
when a private development repository feeds a public collection, package,
monorepo subtree, or file-only export.

## Choose the artifact

Name the exact thing that will become public: the whole target repository, a
specific subtree, or a snapshot copied into a separate publication repository.
Review the source repository and the final public artifact as separate surfaces.
Do not infer the publication boundary from Git tracking, repository layout, or
what an earlier export happened to include.

Inventory top-level directories and meaningful file groups, including hidden
files, docs, config examples, fixtures, agent instructions, local tooling, and
files the packaging/export command would include. Classify each group as:

- **Publish** — needed by public runtime, installation, supported setup,
  documentation, contribution, tests, or licensing.
- **Keep local** — useful for development or operations but not for public users.
- **Remove from source** — proven obsolete or disposable under the normal
  cleanup rules.
- **Needs review** — ownership, value, privacy, or expected public behavior is
  unclear.

Give each decision a short reason in the report. For a curated export, maintain
an exact include/exclude allowlist outside the public artifact when practical.
An unreviewed new path must stop the export until classified. Excluding a file
from an export does not authorize deleting it from the source repository.

## Inspect docs and examples for purpose and provenance

Read every markdown document and inspect every config/example file, but do not
treat inspection as a requirement to publish. In particular, review
`docs/issues/`, `docs/adr/`, backlogs, context notes, changelogs, agent
instructions, local setup notes, and generated reports. These can reveal
development sequence and machine setup even when they contain no secret.
Publish historical context only when it helps the intended public audience;
otherwise keep it local and remove public links that point to it.

Check for local commit hashes and links, references to unpublished branches or
private issues, local usernames and paths, internal project names, host-specific
commands, temporary files, and notes that describe how a feature was developed.
Preserve legitimate authorship, copyright, citations, and contributor credit.
Do not change legal attribution merely to make names disappear.

Inspect config examples individually. Keep only examples that document
supported and useful user setups. A provider-specific endpoint, personal key
source, local proxy, or machine-specific workflow may be valid locally but
unnecessary for the public package. Prefer a neutral default config when it is
sufficient. Keep tests independent of examples when possible: parsing tests
should use small generic fixtures rather than requiring provider-specific
onboarding files. If an example is omitted, update README/setup text, runtime
error messages, and tests so none refer to a missing path.

## Verify the exact public output

Before declaring ready, inspect the output that will actually be published,
not just the source worktree:

- Compare every path in the staged/exported tree with the approved inventory.
  Reject unexpected, missing, ignored-but-packaged, or newly generated files.
- Run secret, privacy, and provenance scans on that exact tree. Search for
  machine paths, private identities, local commit references, internal URLs,
  temporary setup, and secrets without printing matched values.
- Confirm the public README and instructions describe files that are actually
  included, and that the install/test commands work from the public artifact.
- For a separate publication repository, confirm it has an independent Git
  object store with no source alternates or copied source commits.
- Inspect the public destination's reachable history. A new deletion commit
  changes the current tree but leaves earlier files reachable. Report the
  branch/ref and excluded paths still present in its history; do not claim
  those files or secrets are scrubbed.
- If the user asks to remove prior content from published history, follow the
  skill's explicit authorization rule for the exact rewrite tool and refs.
  State that hosting caches, forks, and existing clones may retain copies.

For a simple whole-repository first publication, the approved inventory can be
a concise grouped statement rather than a generated manifest. The checks on
docs, examples, provenance, and the final tree still apply.
