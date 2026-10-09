# Debugging method

Use this when a bug's cause is not obvious after one careful read, when a test
fails only sometimes, or when an obvious fix did not work. The order does not
change: reproduce, list causes, run the cheapest check that tells them apart,
then fix. A fix written before you can reproduce the bug is a guess.

## 1. Reproduce and measure

Write down:

- **The exact command or input** that shows the bug.
- **What happened** — the output, value or error as it came back.
- **What should have happened**, and where that expectation comes from (a
  spec, a test, the owner, the old behaviour).
- **How often** — failures out of runs, e.g. `3/20`. One sighting is a
  report, not a reproduction. For an intermittent bug, run it in a loop first
  and record the rate before you change anything; the same loop proves the
  fix later.
- **Where** — interpreter, dependency versions, OS, parallelism, and anything
  the failing run had that a passing run did not.

If you cannot reproduce it, say so and say what you tried. Do not ship a fix
for a bug you never saw.

## 2. Make sure you are reading the code that ran

Before you trust any reading, confirm the failing process ran the source you
are looking at: print the module path and version from inside the run. A stale
install, an old build directory, a cached bytecode file or a server that was
never restarted all serve old code, and the failure then looks like a bug in
whoever touched the file last.

## 3. List at least three causes on different axes

Three wordings of one guess are one guess. Pick from different axes:

| Axis | Example cause | Cheap check that tells it apart |
|---|---|---|
| Input / state | a value, fixture or cached row is not what you assumed | assert or log the real value at the boundary |
| Order | the result depends on test order or arrival order | run the one test alone, then shuffled |
| Timing | a timeout, sleep or clock read races the work | change the delay and watch the failure rate move |
| Shared resource | two runs share a file, row, port or global | give each run its own and watch the rate drop |
| Build / environment | the running code is not the code you read | see section 2 |
| Dependency | a library or service behaves differently from its docs | pin or stub it and compare |

For each cause write: the claim, the observation that would rule it out, and
which other causes the same observation would also settle.

## 4. Check cheapest-first

Choose the next check by cost first, then by how many causes its result rules
out. Record each cause you rule out and the observed value that ruled it out.
Change one thing per check; two changes at once tell you nothing.

## 5. Intermittent failures and races

- **Passes alone, fails in the suite** — another test leaks state: a global,
  an env var, a module-level cache, a patch left in place.
- **Fails only under load** — a timeout sized for an idle machine, or a
  wall-clock assertion.
- **Parallel workers** — two workers share a temp path, database or port.
- **Lost update** — two writers read the same old value, then both write. Log
  what each writer read.
- **Check-then-act** — a condition true at the check is false at the action.
  Widen the gap with a delay; the rate should rise.
- **The bug vanishes when you add a print or a debugger** — the observation
  changed the timing, so suspect a race. Prefer a counter, an assertion after
  the fact or a recorded trace over another print.
- ⚠️ **A sleep or a retry that makes it pass hides the bug.** Call it a mask
  in your report, never the fix.

## 6. What counts as done

| Claim | What must back it |
|---|---|
| It reproduces | an observed run and its failure rate |
| A cause is ruled out | the observed value that contradicts it |
| The root cause is known | an observation that explains every part of the symptom, including its rate, and one that ruled out each rival |
| The fix works | the same reproduction fails before the change and passes after it, at the same run count |

A bug that stopped after an edit, with no mechanism you can name, is still an
open bug with a different schedule. Say that, and add a regression test that
fails on the old code.
