# Delegate rules (copy, edit, pass via --rules)

Standing constraints for a delegated worker. The CLI prepends this whole file
to the prompt. Keep it short and imperative; per-task specifics go in the task file.

## Reading budget
- Never read a file longer than ~300 lines in full. Grep for the symbol first,
  then read only the relevant line range.
- Keep total file reads under ~20 before your first edit. If you need more,
  the slice is too big — do the smallest correct edit and stop.
- Prefer `grep -n` + targeted line ranges over opening whole files.

## Tests
- Run whole test files: `pytest tests/test_foo.py`. Never a `::`-qualified id
  (`tests/test_foo.py::test_bar`) — a passing single test hides collateral
  breakage in its neighbours.
- Do not add, skip, or xfail tests to make a suite green.

## Report ordering
- Write the REPORT file **before** the final full-suite verification run. A
  late cancel or timeout during that last run must not lose the report.

## Scope
- Do not touch any path outside the stated in-scope list. If a fix seems to
  need an out-of-scope change, stop and say so in the report instead of making
  it.
- No drive-by refactors, formatting sweeps, or dependency changes.

## Data migrations
- For any data migration or destructive transform, dry-run against a COPY of
  the data first. Paste the dry-run output into the report before touching the
  real data.

## General
- Do not commit. Leave the tree for the orchestrator to review and stage.
- Make a reasonable assumption when something is ambiguous and note it in the
  report; do not stop to ask.
