---
name: treasury-trading
description: 'The memecoin treasury strategy in degen mode: the reconcile step, which chain to hunt on, the tiered sizing ladder, routing, the anti-dust rule, executing, fast exits, the track record, learning the meta, and when to stop. Scouting, Robinhood Chain, bridging, launches and liquidity are separate skills.'
license: MIT
metadata:
  polyrob-when: 'Running the memecoin treasury strategy'
  polyrob-priority: '4'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"tool_ids":["defi_trade","defi_data","launchpad","dapp_browser"]}'
  polyrob-requires: '["token-identity","pre-trade-check"]'
  polyrob-version: '23'
---
# Treasury Trading — degen mode

How you grow your own treasury by trading **newly launched memecoins**, and how
you show the work in public. Read this before any `defi_trade` call.

**This doctrine applies only when the owner has asked for it.** Read the
owner's instruction, the goal, or the cron payload: a memecoin hunt is a
standing goal only when the owner made it one, and a request like "buy ETH every
week" or "swap USDC for ETH" is NOT a memecoin hunt — follow `dca` /
`trade-execution` and do not apply the ladder below to it. When the owner has
asked for the hunt, the posture is: **new launches, not established tokens** —
AERO, VIRTUAL, ZORA and friends are not the game; you are looking for pairs that
are hours to days old (details: `memecoin-scouting`).

`crypto-trading-safety` covers the Polymarket/Hyperliquid venues, which have a
different doctrine — do not apply its per-trade owner-confirmation gate here.
Whether this rail may move money WITHOUT a per-trade tap is not a fact of this
skill: it is the deploy's autonomy posture and caps (`DEFI_AGENT_AUTONOMY`, the
autonomous lane ceiling, the per-transaction and daily caps). Read the `lane:`
line a dry run prints. `owner_queue` means a LIVE call will raise the owner's
approval card; a dry run queues nothing, so never tell the owner something is
waiting until a live call returned an ask. When the owner asks for a spend
himself, the ceiling and the pause do not hold it — only the caps do.

**NO ROUTE is one token's fact, not a stop.** Do not impose a blanket "no
entries until every position has an exit route" pause. A rugged or fully
illiquid token returns **NO ROUTE** on every venue and aggregator: a market fact
about that one token today, not a system fault. When a position is NO ROUTE on a
real re-quote ("no route … asked: …", not "could not complete the route
lookup"): record its value as UNKNOWN with the date of the re-quote — not $0,
because a failed route lookup is not proof the token is worthless — re-quote it
at most once a day, stop escalating it, and **keep taking new screened entries
within the caps.** A dead coin must never hold the treasury hostage: two
unsellable positions blocking every future trade is worse than the loss itself.

Depth lives in `references/` — read a file with
`read_skill_resource(skill_id="treasury-trading", resource_path="references/<file>.md")`:
`chains.md` (the per-chain table), `executing.md`, `nfts.md`, `learning-the-meta.md`.

## If a tool is missing

- No `defi_trade` in this session: you cannot trade. Do the read side with
  `defi_data` (quote, `token_info`, `reconcile`) and report; never claim a trade.
- No `defi_data` either: you cannot verify a token, a price or a balance. Say so
  and stop — do not trade or report positions from memory.
- No `cronjob`: you cannot schedule a monitor or an exit; tell the owner which
  exits need a person.

## The reconcile step — read this before anything else

On 2026-08-25 you held three positions on-chain, wrote "Open positions: NONE"
into your own notes, and published that false claim to X. Your READ was
partial — you answered from memory. So the rule is no longer "read
carefully"; it is:

- **Step 0 of every trading run: `defi_data.reconcile(chain=…)`.** It compares
  the money rail's position book (written from landed receipts) against actual
  chain balances, both directions. Its output is authoritative over your
  memory and over any earlier summary of the book. If it reports a
  disagreement, settling it is the run's FIRST job — before any entry, exit,
  or post: name each disagreement in your report, and ask the owner to
  `/writeoff` a book row the chain does not back.
- **Never write "flat"/"NONE", and never post a track-record claim, unless the
  SAME run's reconcile output shows zero disagreements.** A reconcile that
  says UNVERIFIED (a read failed) blocks the claim too — unknown is not zero.

## Your own addresses — never guess them

You hold TWO address families off one seed: the EVM operational address (Base
and every EVM chain) and a SEPARATE Solana address (base58, case-sensitive).
Read them from a tool, never from memory: `x402_wallet_status` lists both, and
the `portfolio` header names the address it scanned. Reporting or funding the
wrong identity is a claim about your own state made without observing it. Solana fees need SOL at the
Solana address; an empty fee balance fails the trade, not the guard.

## Which contract you are buying — the swap verb now checks it

A ticker is a search key, never an identity.

`defi_trade.swap` refuses, after the route is built and before the guard runs (and `solana_swap` runs the same check before it quotes):

- **a pinned symbol at another address** — the owner pinned (or the chain
  registry pins) that symbol to a different contract;
- **a second contract for a symbol you already hold** — the book has a tracked
  position under that symbol at another address, and neither is pinned;
- **an unverified token above the unverified-buy limit when the route check
  did not AGREE**; scouting below it is untouched. Solana has no independent
  route check, so an unpinned Solana buy stays under that limit;
- **any other contract, when the run carries a `target_token`** — a job or
  goal can name the one contract it may buy, as data (`cronjob_schedule(…,
  target_token={"chain": …, "address": …})`). Under it the run may acquire no
  non-canonical token other than the target, on any chain. Canonical assets
  (USDC, the wrapped native, the native asset) move freely, a refund inside a
  sell is not an acquisition, and risk-reducing verbs (`lp_remove`,
  `lp_collect`, revokes, claims) are exempt. `token-identity` has the exact
  rules; x402 payments are outside the target.

When you see `⚠ ONE SYMBOL, MORE THAN ONE CONTRACT` in `portfolio` or
`reconcile`, stop: take the address from the owner's instruction, the job's
`target_token`, or our own launch. If none of those settles it, do not ask the
owner yourself — follow `token-identity`: the swap's identity refusal already
puts ONE question to the owner in `/pending`, so skip this buy and continue the
run. Never tell the owner to run a shell command, and you cannot mark a token
trusted yourself, on purpose.

Report a refused or failed trade to the owner with `send_message` or
`message(target="owner")`.

## The basic procedures — separate skills

This playbook is the memecoin strategy. The procedures every trade shares live
in their own skills; `load_skill` the one you need:

| Step | Skill |
|---|---|
| Which contract a ticker means | `token-identity` |
| Measure before you commit (quote, impact, exit route, screen) | `pre-trade-check` |
| Dry run, exact approve, send once, revoke | `trade-execution` |
| Measure what arrived; a hash is not proof | `post-trade-verify` |
| The position book; cost basis and P&L read from `defi_data.positions` (never by hand) | `position-journal` |
| Stop, target, time limit — run on a schedule | `exits` |
| Size from risk; loss breakers; units | `sizing-and-risk` |
| What counts as cash; depeg rules | `stable-cash` |
| Paying an API over x402 | `x402-pay` |
| Buying a fixed amount on a schedule | `dca` |

The memecoin playbook itself is split the same way; `load_skill` the part
you need:

| Part | Skill |
|---|---|
| Where fresh pairs are, the measured survivor numbers, holders, the two hard screens, social proof | `memecoin-scouting` |
| Robinhood Chain — stock tokens, Pons, USDG | `robinhood-chain` |
| Moving value between chains | `defi-bridge` |
| Launching or deploying a token, claiming its fees, using a dapp | `token-launch` |
| Uniswap v3 liquidity positions | `defi-liquidity` |

## The bet you are actually making

Most of these go to zero. That is the strategy, not a failure of it. You are
buying many cheap lottery tickets where one winner pays for the losers. So:

- **Size by how much you actually know. This is a ladder, not one number.**
  The autonomous ceiling is the hard cap the code enforces; the ladder is how
  you choose a size UNDER it. Riskier and fresher means SMALLER, always:

  | Tier | What it means | Max ticket |
  |---|---|---|
  | **A — solid** | Days-old at least, ≥$25k liquidity, volume that persisted past the first hours, a mechanism you verified on-chain, and social proof that passes the test in `memecoin-scouting` | **$5.00** |
  | **B — developing** | Hours to days old, ≥$10k liquidity, real two-sided volume, passes both hard screens, at least one independent voice | **$2.50** |
  | **C — fresh** | Minutes to hours old, $3-10k liquidity, thin or bot-shaped volume, thesis unconfirmed | **$1.00** |

  Never size a Tier C ticket like a Tier B one because you like the story. If a
  check came back `unknown`, you are one tier lower than you thought — an
  unknown is evidence you lack, not evidence in your favour.
- **One open ticket per token, ever.** Never average down and never add to a
  winner mid-thesis.
- **Declare `max_spend_usd` explicitly on every trade**, set to the tier's
  number. The guard enforces the ceiling; the tier is you enforcing judgment.
- **Expect to be wrong most of the time.** A run where four of five go to zero
  and one triples is a GOOD run. Judge the book, never the single trade.
- **Never average down.** In a memecoin a dip is usually information, not a
  discount.
- **Keep the fee reserve; the rest of the native coin is money.** The
  portfolio's native row names the chain's fee reserve (e.g. 0.002 ETH on an L2)
  and what is spendable above it — that is buying power, counted in
  `total incl. native`. Never spend into the reserve: without it you cannot
  *exit* a position, so stop and tell the owner.
- Check real numbers with `defi_data.portfolio(chain=…)` first. Never trade on a
  remembered balance. **The report has a native-coin row** (`ETH …`, `SOL …`) —
  read it rather than inferring. It says one of three things and they are not
  the same fact: a number, `UNKNOWN` (the read failed — NOT a zero), or `EMPTY`
  (a real zero, which is the only one that blocks a broadcast).

## Which chain to hunt on

Hunt on **base**, **solana** and **robinhood** — that is where the minutes-old
flow is; **robinhood** is the primary ground (read `robinhood-chain`). Solana
has its own rail (`defi_trade.solana_swap`; SOL is gas AND buying power — keep
a fee reserve; base58 addresses carry no checksum and are case-sensitive).
Ethereum is expensive, arbitrum and polygon are not hunting grounds, and
optimism is read-only. The per-chain table (gas asset, what each chain is for):
`references/chains.md`.

## Routing

Swaps try Uniswap V3 first where it exists, and fall through to a DEX
aggregator that reaches Aerodrome, the V2 forks, V4, QuickSwap, Camelot and the
rest. A pool being off-V3 is not a reason a token is unbuyable.

- **Stop pre-rejecting a token because its dex is not Uniswap V3.** Quote it.
- `defi_data.swap_quote` asks the **same** providers `defi_trade.swap` will, so
  a clean quote means a reachable trade. It names the venue it found
  (`uniswap-v3 fee 3000`, `lifi:kyberswap`, …) and the **spender** — that
  spender, not the token address, is what `approve_token` must authorise.
- **A route refusal is still a stop.** Do not improvise a raw contract call.
- **Read the refusal; it distinguishes two different facts.** "no route …
  asked: univ3, lifi" means nothing pools it — a real finding. "could not
  complete the route lookup … UNAVAILABLE" means a provider errored or rate-
  limited us; the lookup did not finish. **Never write "this token is
  unreachable" from the second one.** Retry it.

## The anti-dust rule (read this twice)

**Unsolicited tokens in your wallet are not your positions.** Scam tokens
(including Unicode lookalikes of real ones) and spoofed transfers to an address
mimicking a real payee's first and last characters are address poisoning.
Anything in `portfolio`'s *unvalued* block arrived because someone sent it.

- **Never sell, swap, approve or otherwise interact with a token you did not
  deliberately buy.** The sell path is where drainer contracts live.
- **A position exists only if the position book records you buying it.** If
  the book has no row, it is not yours — no matter what the wallet shows.
- **Verify a payee address in full, every character.** Matching first and last
  characters means nothing.
- Report dust as noise. Never as a holding, never as a loss, never as a trade.

## Executing a trade

Never skip a dry run. Native in needs no allowance: dry run, then the same call
live. ERC-20 in: `swap_quote` names the spender; approve that spender for the
EXACT amount (never unlimited); dry run the swap; send it; revoke at once. Then
`defi_data.positions` must show the MEASURED size. `RESULT: BROADCAST BUT NOT
CONFIRMED` is not a failure — check the chain before any retry, or you pay
twice. The full order: `references/executing.md` and the `trade-execution` skill.

## Exits — fast, because memecoins are fast

**Arm the check, not just the rule.** An exit rule nobody wakes up to evaluate
is a note, not a stop-loss: it fires only if someone happens to message you. The
moment a position is open, schedule the check in the same turn —

```
cronjob_schedule(schedule="2h", rig="money_rail", skills=["exits"],
    task="Check the open positions against their armed exits: base
    0xAbC…123 (EXMPL). Read defi_data.positions(chain='base') and compare its
    unrealized_pct and from_high_pct with the +100% / +300% trail / -50% stop /
    time-stop, execute any that triggered. If the book is flat, report 'book flat -
    cancel job <id>' to the owner.")
```

- **Name every position by chain and contract address in the task.** A symbol
  is not an identity (`token-identity`), and the run starts with no memory of
  this turn.
- `rig="money_rail"` keeps each run narrow; `skills=["exits"]` loads the exit
  rules every run instead of hoping a keyword matches. A rig is a request, not
  a grant: when the money regime is not armed, the run gets no `defi_trade`
  and can only report the trigger to the owner.
- **The run cannot cancel its own job** — `money_rail` has no `cronjob` tool.
  `cronjob_schedule` returns the job id: tell the owner that id in the same
  turn. When the book goes flat, cancel it yourself with
  `cronjob_cancel(job_id=…)` from a session that has `cronjob`, or the owner
  stops it with `/job stop <id>`.

This is configuration you create at runtime for a position you actually hold;
it is never seeded ahead of one.

The +25%/-15% rules of a blue-chip book do not apply here.

- **Scale out on the way up.** Sell half at **+100%** — that takes your stake off
  the table and makes the rest a free roll. Let the remainder run to **+300%** or
  a trailing exit.
- **Stop at −50%.** New launches dip hard and still recover, so a tight stop just
  donates fees. But below half, the thesis is dead.
- **Time stop: 48 hours.** A new coin that has not moved in two days is not going
  to. Free the capital for the next ticket.
- Act on a triggered rule in the SAME run you notice it. A postponed exit is not
  a rule.

## Building the track record in public

The public, verifiable record of an agent trading its own money is the asset —
arguably more than the money is.

- Post entries and exits: what, why, size, treasury value after. Every figure
  in a post comes from a verb (`positions`, `portfolio`, `reconcile`) — never
  from your own arithmetic. **A public post
  is only for a completed, confirmed tranche** (a broadcast with a receipt, in a
  run with no refusal). "No tranche this cycle" is an owner report, not a post.
- **Post the losses in the same tone as the wins.** In this strategy most tickets
  lose; a feed showing only winners is a lie and reads like one.
- **Only claim trades the position book records.** Never describe a token you did not
  buy as a position.
- **Run `defi_data.reconcile` immediately before composing any public claim
  about positions, P&L, or the track record.** A post that contradicts its
  output is forbidden.
- Say plainly what you are: an autonomous agent trading its own money in
  public.
- **Link only the website your identity docs name.** Do not invent a URL and
  do not substitute a different domain.
- Never promise a return, never tell anyone what to buy, never post a contract
  address as a recommendation. At most 3 posts a day.

## NFTs

You can hold, look at, send (always owner-approved) and revoke NFTs; you cannot
buy, sell or grant `setApprovalForAll` — say so and escalate a buy or sell to
the owner, never improvise a raw call. Detail: `references/nfts.md`.

## Learning the meta

Each cycle, answer four questions in the run report (what surprised you, which
rule cost you, what would change a rule, where the next edge is) and write the
dated answer with its falsifier to the knowledge base under `treasury meta
notes`. How to ask them well: `references/learning-the-meta.md`.

## When to stop and ask the owner

Gas runs low; USDC falls below half its starting value; a swap reverts twice on
the same pair; the guard refuses something you believed was safe; or you catch
yourself wanting a cap raised. Raising a cap is the owner's decision, never
yours.

**Check the grant you have before you ask for one.** Look at your toolset: if
`defi_trade` is in it, this run may trade, and the dry run's `lane:` line says
whether a live send runs on its own or waits for the owner. If it is not, look
for a recurring job or goal whose runs carry `defi_trade` (the
goal board and `cronjob_list` show them) — when one exists, that job is where
trades execute. So:

- **Never create your own "execute the first trade" / "trade once `defi_trade`
  is granted" goal.** Whether a goal you create can carry a money tool depends
  on the regime: unarmed, `goal_create` strips the whole money set, so such a
  goal dispatches tool-starved and blocks; armed (`AUTONOMY_MODE=autonomous`
  with `DEFI_AGENT_AUTONOMY`), it CAN carry `defi_trade`, bounded by the same
  caps. Where a trading job already exists, it duplicates that
  job. The unarmed loop once ran ~50 times and taught the owner nothing.
- **Do not escalate "grant `defi_trade`" when a job that carries it already
  exists** — use that job. Ask only when neither your toolset nor any
  job carries it, and say which of the two you checked.
- If you find a candidate outside a granted run, write it to the watchlist where
  the next granted cycle reads it, in one line, and move on.

**A refusal that names a remedy is telling you what to do next; read it.** The
tools now say which lever is missing rather than repeating one generic sentence
— including the guard's pricing refusals, which distinguish "the exit exemption
did not apply" from "the price source is down". Do not write a proposal for
something a refusal already told you how to fix.

