# Review lenses

Use this when you review a diff — your own before you ship it, or one the owner
hands you. A single read-through finds the first kind of problem it looks for
and then stops looking. Read the same diff several times, each time asking ONE
question. Write down which lenses you actually ran: a review that ran two of
the five is a two-lens review, and you say so.

## Order

Run the lenses in this order. If you can afford only one, run the first.

1. Verification gap
2. Break it
3. Boundaries
4. Shape
5. Words

## 1. Verification gap — "if this broke tomorrow, would a test go red?"

This lens does not ask whether the code is right. It asks whether anything
would notice if it stopped being right.

1. **Is the behaviour changed at all?** A rename, a reformat, a comment or a
   type-only edit that cannot change a return value, an error, a side effect
   or stored state has no gap — report "no behavioural change" and stop. The
   one exception: a change that deletes, skips or weakens a test IS in scope.
2. **List each changed behaviour separately**: the value returned, the branch
   taken, the error raised, the shape written to disk or the wire, a default,
   a validation rule. A dependency bump or a data-file edit counts as
   behaviour even when no logic line moved.
3. **Follow each one to where something observes it** — the callers, the
   registered entry points, whoever reads the file, row or message. Stop at
   the first place a test would fail, or where the next hop is a guess.
4. **Invent the smallest regression** a caller would see — flip the branch,
   drop the default, omit the field, return the old error — and read the test
   that should catch it. Would one of its assertions fail?

Report three kinds of gap:

- **Unguarded change** — the behaviour can regress and no test that runs
  would fail.
- **Missed adopter** — the change replaces an old way of doing something, a
  call site still does it the old way, and nothing flags it. Only report this
  when the change itself shows the old way is meant to go; otherwise it is a
  refactor idea, not a gap.
- **Hollow test** — a test looks like it covers the behaviour but would pass
  anyway: it is skipped, it is outside the normal run, it only checks that
  nothing raised, it asserts on a mock call, it mocks away the thing under
  review, or it runs the path without looking at the changed output.

A test counts only if it runs in the normal test command AND an assertion reads
the changed value.

⚠️ **Search before you say "untested".** Grep for the symbol, its import and
its string literals across the test tree first. Then say how far you looked:
"none of the tests that import `x` cover the new branch" is a finding; "there is
no test" is a claim about the whole repository and needs that search behind it.

Do not rank gaps by severity. A gap is there or it is not; the answer is a
test, not an argument.

## 2. Break it

Read the diff as someone who wants it to fail: hostile input, a dropped
connection, a retry that runs twice, a caller who passes `None`. This lens must
end with at least one finding OR a short list of what you attacked and why it
held. "Looks fine" is not a result.

## 3. Boundaries

Walk every branch and edge the change adds: empty, one, many, the maximum,
missing, malformed, concurrent, and the error path out of each. On our money
and messaging rails, also check: what happens when the call half-succeeds (sent
but no receipt, broadcast but unconfirmed)?

## 4. Shape

Compare the shape of the change with the shape of the codebase. Is the logic
in the right layer (`core <- modules <- agents <- tools <- surfaces`)? Does it
add a second copy of something that already has one owner (a second status
reader, a second env parser, a second verb table)? Will the next change have to
undo it?

## 5. Words

Comments, docstrings, log lines, commit message, the report to the owner. A
comment that claims more than the code guarantees is a real finding, not a
nit: the next reader trusts the comment instead of the code.

## Writing the findings

- Group findings under the lens that found them.
- For each: the file and line range, what is wrong, the smallest input or
  sequence that shows it, and a fix if one is obvious.
- Lenses 2-5 carry a priority: **P0** loses money, data or a secret, or breaks
  every run; **P1** a real user path is wrong; **P2** an edge case or a
  maintainability cost; **P3** polish. Say how sure you are when you are not.
- Drop any finding you cannot point at in the code you read.
- End with two short lists: **checked and clean** (what you looked at and
  found nothing) and **not looked at** (what you skipped and why). The second
  list is what stops a partial review from reading as a complete one.
