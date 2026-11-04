---
name: coding-workflow
description: Edit code safely (read, plan, edit, run tests, iterate, record)
license: MIT
metadata:
  polyrob-when: 'Editing code: read, plan, edit, run tests, iterate'
  polyrob-priority: '3'
  polyrob-triggers: '{"tool_ids":["coding","code_execution","filesystem"]}'
  polyrob-version: '3'
---
# Coding Workflow

Edit code safely: read, plan, change, test, iterate. Recommended workflow — adapt it.

## Tool availability
Read the `coding` line of your `<tool-catalog>`. `[loaded]` → use it. `[loadable]` →
call `load_tool("coding")` first. `[gated:…]` → follow the remedy it names (see
`harness-manual`). A delegated sub-agent never holds `coding`. Without `coding`, edit
with `filesystem_read_file` / `filesystem_write_file` and state how to verify.

The `coding` verbs:
- `coding_grep` — search the tree (a regex, a `glob` filter).
- `coding_str_replace` — one exact edit (unique `old_string`, or `replace_all`).
- `coding_apply_patch` — a multi-hunk unified diff for one file.
- `coding_create_file` / `coding_move_file` / `coding_delete_file`.
- `coding_run_tests` — run the project's tests.
- `coding_snapshots` / `coding_restore` — only when snapshots are on (the schema says
  so); otherwise undo with `git_diff` and `git_checkout`.

## When to use
Fixing a bug, implementing or refactoring code, adding a function, or running tests.

## Workflow
1. **Read before you write.** `coding_grep` the symbol and read the surrounding file so
   the change fits the existing code.
2. **Plan the smallest diff** that solves the problem; match local conventions and
   comment density. Don't gold-plate.
3. **Make the edit.** Prefer `coding_str_replace` for one exact edit and
   `coding_apply_patch` for several hunks. With `filesystem`, write content
   **verbatim** — never strip or reflow indentation. After an edit, read any
   `<diagnostics>` block the result carries.
4. **Iterate** on failures — read the error, fix, re-run — don't guess.
5. **Run the tests LAST.** `coding_run_tests` returns the tail of the output and a log
   path; read the log with `filesystem_read_file` when the tail is not enough. An
   edit after a green run voids it: a deploy verb (`app_service_deploy`,
   `hf_deploy_deploy`) refuses a tree with no green run since the last edit (ship ==
   tested). If no test tool is available, state exactly how to verify by hand.
6. **Report and record.** Name the files changed and the test result. In a git repo,
   follow `git-and-pr-flow`: `git_status` → `git_diff` → `git_add` (named paths) →
   `git_commit`. Push only when the owner asked; a push is public.

## Notes
- Touch only what the task needs; leave unrelated code alone.
- A long command, a dev server or a background job: see `shell-and-sandbox`.

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
