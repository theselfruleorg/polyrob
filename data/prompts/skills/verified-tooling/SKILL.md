---
name: verified-tooling
description: 'How to build a small read-only analysis tool you can trust and re-run: one tool one question, pure and read-only, UNKNOWN is never zero, tests alongside, a real run recorded in a smoke log, and cross-checked totals'
license: MIT
metadata:
  polyrob-when: 'Building a small read-only analysis tool to re-run'
  polyrob-priority: '3'
  polyrob-triggers: '{"tool_ids":["coding","code_execution"]}'
  polyrob-version: '3'
---
# Verified Tooling — small tools you can trust twice

When you need the same analysis more than once — a weekly summary of a log, a metric
diff between two exports, a check over a folder of records — build a small tool
instead of re-parsing by hand each session. Re-parsing by hand is not cheaper; it is
the same work with no record of whether you got it right last time.

The rules are about TRUST, not size.

## The verbs
Build the tool with `coding_create_file` (or `filesystem_write_file` without
`coding`), test it with `coding_run_tests`, and run it with
`code_execution_run_code` or `shell_run` where that tool is loaded. Without a way to
run code, say so: a tool you cannot run is not verified.

## The rules

1. **Pure and read-only.** The tool reads a file and writes a report. No network,
   no writes outside its output file, no side effects. Fetch live data separately
   with the verbs and pass it in as an input file. A tool that reaches the network
   cannot be re-run on yesterday's data to check yesterday's answer.

2. **UNKNOWN is never zero.** If the input does not record a value as a clean
   number, emit `null`/`UNKNOWN` and EXCLUDE it from sums. A missing reading is not
   a zero reading; a blank cell is not "none". A total that quietly absorbed an
   unknown as 0 is wrong in the direction that looks responsible, which is the
   worst direction.

3. **One tool, one question.** `log_summary` (counts per level and per day),
   `metric_diff` (added / dropped / changed between two exports),
   `records_check` (rows that fail the schema). A tool that answers three questions
   is three tools you cannot test.

4. **Tests alongside, covering the parse edge cases.** Missing columns, an empty
   file, a duplicate row, a malformed date, a value in the wrong unit. The edge
   cases are the whole value: the happy path was never the thing you would have
   got wrong.

5. **Verify with a real run, then record it.** Append a dated entry to a
   `SMOKE.md` next to the tool: the test count, the exact command, a summary of
   the output, and an honest note on scope and limits. "It works" with no
   command and no output is not a verification, and next month you cannot tell
   whether it ever ran.

6. **Cross-check overlapping totals.** If two tools compute figures that must
   agree — a per-day total summed against a whole-period total — assert it.
   Agreement between two independent paths is the strongest correctness evidence
   available without an oracle.

7. **Key rows by a stable identifier** (an id, an address, a path), never by a
   display name: two records share a name more often than you expect.

## What this is not

It is not a place to put an action. A tool that sends, spends or changes outside
state is not a small read-only tool, and none of the trust above transfers to it.
Actions go through their own verbs, with their gates and their caps, every time.
