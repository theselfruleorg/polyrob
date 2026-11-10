---
name: dca
description: 'Dollar-cost averaging: one trusted major bought for a fixed USD amount on a schedule, set up as ONE scheduled job (the dca trading preset cannot run a DCA yet), the per-run judgment (peg, gas, suspect quote, never double a buy) and the stop rules. There is no native recurring-order verb.'
license: MIT
metadata:
  polyrob-when: 'Buying a fixed amount on a schedule (DCA)'
  polyrob-priority: '3'
  polyrob-triggers: '{"tool_ids":["defi_trade","defi_data","cronjob"]}'
  polyrob-requires: '["token-identity","pre-trade-check"]'
  polyrob-version: '4'
---
# DCA — buy a fixed amount on a schedule

Dollar-cost averaging buys the same USD amount of one asset at a fixed
interval, whatever the price. It needs no prediction, and it is the safest
strategy to run unattended. Each run of the DCA job pins this skill. The
numbers (per-buy USD, cadence, total, end date) live in the job's task text,
never in this skill.

There is no native DCA or recurring-order verb on the on-chain rail: the
job's schedule IS the strategy, and each buy is a `defi_trade.swap` (or
`solana_swap`).

## Set it up — one scheduled job

1. **Confirm the target by ADDRESS** (`token-identity`). DCA buys one major
   only — the wrapped native or another major the owner chose, never a fresh
   launch. The target must be trusted: `token_info` says `verified` (a
   canonical pin, or the owner's `/wallet trust`). Tell the owner the address
   you will buy.
2. **Schedule ONE job in the owner's turn.** The `dca` trading preset cannot
   run a DCA today: its hunt wakes only when discovery finds a new candidate
   (discovery skips a token you already hold), and its one slot refuses every
   buy after the first. Until that is fixed, use one job:
   `cronjob_schedule(rig="money_rail", skills=["dca","trade-execution"],
   target_token={…})`; its task text holds the per-buy USD, the total, the
   cadence and the end date. One job only — no second "cancel" job: at the end
   date or the total, the run stops buying and tells the owner to cancel it.
   The call raises a card: the job exists only after the owner confirms it.
3. **Tell the owner** the address, the per-buy USD, the cadence, the total and
   the end date, and that `/jobs` shows and stops the job.

## Each run — the judgment only

The money guard in code still refuses a spend over the caps. You judge this:

1. **The target.** Buy only the job's `target_token` — the one trusted
   address the owner confirmed at setup. If you cannot name ONE trusted address, buy nothing and say so.
2. **The paying stable holds its peg** (`stable-cash`): below $0.995, skip
   this run.
3. **Gas is under 1% of the buy** (`pre-trade-check` step 1). Else skip.
4. **The quote is clean**: `defi_data.swap_quote` at the ticket. A
   `SUSPECT QUOTE` skips this run.
5. **Buy the per-buy USD** the job's task states, through
   `trade-execution`, with a `plan` (thesis: "DCA buy of <address>",
   invalidation: the peg or the target's identity fails). Then
   `post-trade-verify`.

Never double the next buy to make up a skipped one. A skip is one line in the
report with its reason.

## Stop rules — report, never work around

- the total is spent or the end date is past: buy nothing and tell the owner
  to cancel the job (`/jobs`);
- the owner says stop: the owner cancels the job;
- the paying stable is below $0.98: report it to the owner;
- two runs in a row could not buy — report why instead of retrying.

## If a tool is missing

Follow `harness-manual`; do the read steps you can, and never work around a
missing verb with a raw `call`, a browser dapp or another agent.

## Related

`trading-mandate`, `token-identity`, `pre-trade-check`, `trade-execution`,
`position-journal`, `stable-cash`.
