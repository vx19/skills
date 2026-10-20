# Task — <one phase> (copy, fill in, pass via --task)

One phase = one dispatch. Fill every section. If you cannot, the slice is too
big or too vague — shrink it until you can.

## Objective
<1-2 lines: the one concrete outcome of this phase.>

## In-scope paths
<Files/dirs the worker may change. Mirror these into --scope (an allow-list)
so out-of-scope changes are reported in the envelope.>

## Do-not-touch paths
<Paths that must not change. Keep these constraints in the prompt; --scope
is an allow-list, not a deny-list.>

## Reading budget
<The specific files and line ranges the worker should read — not "the codebase".
Keep it small; the rules file caps total reads. Naming the exact files and
functions shortens the worker's time to first edit.>

## Acceptance
<Exact test commands, whole-file (no ::-qualified ids). These must pass.>

## Invariants
<Domain rules the change must preserve, phrased so a test can assert them.>

## Dry-run
<For any data migration: the dry-run command and the path to the data COPY to
run it against. Paste the output into the report before the real run.>

## REPORT
<Absolute path to write the report to. Write it BEFORE the final verification
run. Also pass this path as --report on the command line.>

<!--
FILLED EXAMPLE
===================
## Objective
Add a --dry-run flag to scripts/export.py that prints planned output paths and
exits 0 without writing.

## In-scope paths
scripts/export.py, tests/test_export.py

## Do-not-touch paths
config/, other CLI flags, README.md, any file under data/

## Reading budget
scripts/export.py lines 1-60 (argparse) and 180-240 (write loop);
tests/test_export.py lines 1-40 (fixtures). Nothing else.

## Acceptance
python -m pytest tests/test_export.py
scripts/export.py --dry-run exits 0 and prints at least one "would write: ..." line

## Invariants
--dry-run writes zero files; existing (non-dry) behaviour is byte-identical.

## Dry-run
N/A (no data migration this phase).

## REPORT
/path/to/repo/.reports/export-dry-run.md
(write before running the full pytest suite)
-->
