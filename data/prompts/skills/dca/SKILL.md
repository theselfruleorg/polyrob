---
name: dca
description: 'Dollar-cost averaging on a schedule: majors only, an owner budget and end date, the target by address, cronjob_schedule with the money_rail rig and pinned skills, the per-run steps and the stop rules. There is no native recurring-order verb.'
license: MIT
metadata:
  polyrob-priority: '3'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["dca","dollar cost averaging","dollar-cost averaging","recurring buy","accumulate weekly","accumulate daily"],"task_patterns":["(buy|accumulate)\\b.*\\b(eth|sol|btc|wbtc|usdc|token|coin|crypto)\\b.*\\b(every|each) (day|week|month)","(buy|accumulate)\\b.*\\b(every|each) (day|week|month)\\b.*\\b(eth|sol|btc|wbtc|token|coin|crypto)\\b"],"tool_ids":["defi_trade","defi_data","cronjob"]}'
  polyrob-version: '2'
---
# DCA — buy a fixed amount on a schedule

Dollar-cost averaging buys the same USD amount of one asset at a fixed
interval, whatever the price. It is the simplest strategy that needs no
prediction, and the safest one to run unattended.

## Where it fits

- **Assets:** majors only — the wrapped native or ETH, a pinned token the owner
  chose. Never a fresh launch.
- **Budget:** a total, a per-buy amount, and an end date, all set by the owner.
- **Venue:** `defi_trade.swap` on a schedule. There is no native DCA or
  recurring-order verb on the on-chain rail yet — the schedule IS the strategy.

## Set it up

1. **Confirm the target by ADDRESS** (`token-identity`). The job must carry the
   address, not the symbol. On 2026-09-25 a recurring buyback whose text had
   lost the address bought a look-alike — confirm the address with the owner
   before the first run. A target the owner writes into the job is trusted for
   that job; nobody has to pin it. If the identity check refuses a buy anyway,
   it has already asked the owner in `/pending` — do not message him about it.
2. **Schedule it with the target as data:** `cronjob_schedule(task=…,
   schedule="every monday 09:00", rig="money_rail", skills=["dca",
   "trade-execution"], target_token={"chain": "base", "address": "0x…"})`.
   Under `target_token` every run may acquire no non-canonical token other than
   the target, on any chain; canonical assets (USDC, the wrapped native) move
   freely (`token-identity` has the exact rules). The task
   text still names the chain, `token_in` (usually `native` or pinned USDC),
   the per-buy USD, the total budget, the end date, and the stop rules below. A
   rig is a request, not a grant: if the job's runs cannot reach `defi_trade`,
   the owner must grant it — say so instead of working around it.
3. **Schedule its end in the same session.** A `money_rail` run holds no
   `cronjob` tool, so the buy job cannot cancel itself. From the session that
   scheduled it (which does hold `cronjob`), add a one-shot job at the end date:
   `cronjob_schedule(task="cancel DCA job <id>: cronjob_cancel(job_id=<id>)",
   schedule="<ISO end timestamp>", rig="ops")` — the `ops` rig carries
   `cronjob`. Both rigs are honoured when the OWNER asks for the DCA in chat; a
   job you create on your own is refused a rig outside your ceiling ("Tool rig
   '…' NOT granted"). Then give the owner the job id and the end date instead.
4. **Write the plan into the ledger** (`position-journal`) with the budget,
   the running total spent and the job ids.

## Each run

1. `defi_data.portfolio` — enough of `token_in`, and gas for this buy.
2. Budget left? Sum the spent total from the ledger. If this buy would pass the
   total or the end date is past, do NOT buy: report "DCA budget spent — cancel
   job <id>" to the owner. The run cannot cancel the job itself.
3. `pre-trade-check` in short: quote, cross-check, gas under 1% of the buy.
   Skip this run (do not double the next one) if the quote is `SUSPECT`, the
   stable is off its peg (`stable-cash`), or gas is above 1% of the buy.
4. `trade-execution` with `max_spend_usd` = the per-buy amount, then
   `post-trade-verify`, then add the fill to the ledger.

## Stop rules

- the total budget is spent, or the end date passed;
- the owner says stop;
- the paying stable is below $0.995;
- two runs in a row could not trade — report why instead of retrying.

## If a tool is missing

These steps need `defi_data` (reads) and `defi_trade` (trades) in THIS session.
If `defi_trade` is not loaded, do the read steps, write the plan, and tell the
owner which grant is missing. If `defi_data` is not loaded either, you cannot
measure anything — say so and stop. Never work around a missing verb with a raw
`call`, a browser dapp, or another agent.

## Related

`token-identity`, `pre-trade-check`, `trade-execution`, `position-journal`,
`stable-cash`.
