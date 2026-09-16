---
name: verified-tooling
description: 'How to build a small read-only analysis tool you can trust and re-run: one tool one question, pure and read-only, UNKNOWN is never zero, tests alongside, a real run recorded in a smoke log, and cross-checked totals'
license: MIT
metadata:
  polyrob-priority: '3'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":["coding_create_file","coding_run_tests","code_execution_run_code"],"keywords":["build a tool","small tool","script","analysis tool","parser","ledger summary","pnl curve","watchlist diff","report tool","re-run","smoke test","verify the tool"],"task_patterns":["(build|write|make).*(tool|script).*(report|summar|analy|parse)","(re-?run|repeat).*(analysis|report)"],"tool_ids":["coding","code_execution"]}'
  polyrob-version: '1'
---
# Verified Tooling — small tools you can trust twice

When you need the same analysis more than once — a ledger summary, a P&L curve,
a watchlist diff, a screen over candidate data — build a small tool instead of
re-parsing by hand each session. Re-parsing by hand is not cheaper; it is the
same work with no record of whether you got it right last time.

This is the pattern that worked in practice. It is about TRUST, not size.

## The rules

1. **Pure and read-only.** The tool reads a file and writes a report. No chain
   calls, no trades, no network inside it. Fetch live data separately with the
   verbs and pass it in as an input file. A tool that reaches the network cannot
   be re-run on yesterday's data to check yesterday's answer.

2. **UNKNOWN is never zero.** If the input does not record a value as a clean
   number, emit `null`/`UNKNOWN` and EXCLUDE it from sums. Indexer lag is not
   zero liquidity. A missing exit price is not a loss of 100%. A total that
   quietly absorbed an unknown as 0 is wrong in the direction that looks
   responsible, which is the worst direction.

3. **One tool, one question.** `ledger_summary` (open/closed + totals),
   `pnl_curve` (per-trade cumulative), `watchlist_diff` (added/dropped/carried).
   A tool that answers three questions is three tools you cannot test.

4. **Tests alongside, covering the parse edge cases.** Missing columns, rows with
   no address, a dead pool (that is a CLOSED position, not a stuck one), a
   duplicate row. The edge cases are the whole value: the happy path was never
   the thing you would have got wrong.

5. **Verify with a real run, then record it.** Append a dated entry to a
   `SMOKE.md` next to the tool: the test count, the exact command, a summary of
   the output, and an honest note on scope and limits. "It works" with no
   command and no output is not a verification, and next month you cannot tell
   whether it ever ran.

6. **Cross-check overlapping totals.** If two tools compute figures that must
   agree — a P&L curve's final total against a ledger summary's — assert it.
   Agreement between two independent paths is the strongest correctness evidence
   available without an oracle.

7. **Key rows by contract address when there is one** (EVM lowercased, Solana
   base58 as written), else `chain:symbol`. A ticker is not an identity; two
   tokens share one every week.

## What this is not

It is not a place to put a money verb. A tool that can spend is not a small
read-only tool, and none of the trust above transfers to it. Spending goes
through the money verbs, with their gates and their caps, every time.
