---
name: coding-workflow
description: Edit code safely (read, plan, edit, run tests, iterate)
license: MIT
metadata:
  polyrob-priority: '3'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["fix","implement","refactor","add function","bug","write code","run tests","debug","edit code"],"task_patterns":["fix.*bug","implement.*","refactor.*","add.*function","write.*code","run.*tests","debug.*"],"tool_ids":[]}'
  polyrob-version: '2'
---
# Coding Workflow

Edit code safely: read, plan, change, test, iterate. Recommended workflow — adapt it.

## Tool availability
The `coding` tool (`str_replace` / `grep` / `run_tests`) is available under local mode
(POLYROB_LOCAL); it is **not loaded by default on the server**. If `coding` is unavailable,
use `filesystem` read/write to make the edits and state how to verify them.

## When to use
Fixing a bug, implementing/refactoring code, adding a function, or running tests.

## Workflow
1. **Read before you write.** `grep` the symbol and read the surrounding file so the
   change fits the existing code.
2. **Plan the smallest diff** that solves the problem; match local conventions and
   comment density. Don't gold-plate.
3. **Make the edit.** Prefer `coding.str_replace` for exact edits. With `filesystem`,
   write content **verbatim** — never strip or reflow indentation.
4. **Run the tests** (`coding.run_tests`) after each change; if no test tool is
   available, state exactly how to verify manually.
5. **Iterate** on failures — read the error, fix, re-run — don't guess.
6. **Report** the files changed and the test result.

## Notes
- Touch only what the task needs; leave unrelated code alone.

## Deeper methods — read one only when the step needs it
Load with `read_skill_resource(skill_id="coding-workflow", resource_path="references/<file>.md")`.
- `review-lenses.md` — before you ship a non-trivial diff, or when asked to
  review one. Its first lens asks which claim has no test behind it.
- `build-failure-triage.md` — a build, install, lint or CI run fails with
  more than one error.
- `debugging-method.md` — the cause is not clear after one careful read, a
  test fails only sometimes, or a first fix did not work.
- `cleanup-passes.md` — after the change works and is green, to remove dead
  code, copies and needless layers without changing behaviour.
