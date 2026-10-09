# Cleanup passes

Use this AFTER a change works and its tests pass, when the diff carries
leftovers — dead code, copies, needless layers — or when the owner asks you to
tidy an area. Cleanup must not change behaviour, so each pass does one kind of
edit and the tests run between passes. A diff that deletes, renames and moves
at once cannot be reviewed for "same behaviour", and reverting one mistake
reverts all of it.

## Scope

- The files the change touched — or the list the owner gave — are the whole
  area. List anything you notice outside it under "out of scope" in the
  report; do not edit it.
- Run the tests that cover the area once BEFORE the first pass and record the
  result. That run is the baseline every pass is compared against.

## First, sort what you found

| Kind | Looks like | Default action |
|---|---|---|
| Dead code | unused imports and symbols, unreachable branches, commented-out blocks, a flag nothing reads | delete it; no shim for code with no callers |
| Copies | the same decision written twice; near-identical helpers that have drifted | keep one owner, point callers at it; lookalikes that encode different decisions stay |
| Needless layers | a single-use helper, an interface with one implementation, a wrapper that only forwards, config for a value that never varies | inline it; extract again only when a second real caller appears |
| Wrong place | logic in the wrong layer, reaching into another module's internals, an import cycle | fix through the existing public surface; moving a boundary is a design change — ask first |
| Missing locks | behaviour with no test, a test that asserts nothing | add the test in pass 4 |
| Filler | comments that restate the code, placeholder docstrings, unused config blocks | delete; keep a comment only if it says what the code cannot |

If nobody named the smells, find candidates with the repo's own tools first
(its lint gate, e.g. `ruff check`; `vulture` or `knip` where installed) and a
grep for commented-out blocks. A tool hit is a candidate, not a verdict: grep
for callers (including string-based lookups, registries and tests) before you
delete anything.

## The passes, in order

1. **Delete dead code.** Deletions only — no renames, no moves. The diff is
   almost all red. Run the tests.
2. **Collapse copies.** One owner per decision; move callers to it. Run the
   tests.
3. **Fix names and error handling.** Rename what misleads; stop swallowing an
   error that should surface. No structural moves. Run the tests.
4. **Lock the behaviour.** Add the tests that passes 1-3 showed were missing;
   delete tests that assert nothing. Run the tests.

⚠️ If a pass turns the tests red, fix or revert THAT pass before you start the
next. Never carry a red run forward.

## Report

1. **Files changed**, with a count per pass.
2. **What went**, by kind (dead code, copies, layers, filler).
3. **Proof of same behaviour** — the test command before the first pass and
   after the last, with both results.
4. **Left alone** — what you found and chose not to touch, and why
   (out of scope, needs a design decision, no test to protect it).
