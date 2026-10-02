---
name: treasury-trading
description: 'The memecoin treasury strategy in degen mode: the ledger and reconcile step, which chain to hunt on, the tiered sizing ladder, routing, the anti-dust rule, executing, fast exits, the track record, learning the meta, and when to stop. Scouting, Robinhood Chain, bridging, launches and liquidity are separate skills.'
license: MIT
metadata:
  polyrob-priority: '4'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":["defi_trade_swap","defi_trade_solana_swap","defi_trade_approve_token","defi_trade_revoke_approval","defi_trade_unwrap","defi_data_portfolio","defi_data_positions","defi_data_reconcile","defi_data_token_resolve","defi_data_swap_quote"],"keywords":["treasury trading","grow the treasury","treasury wallet","buy token","sell token","on-chain trade","memecoin","meme coin","degen","robinhood","robinhood chain","aerodrome","uniswap","trading ledger"],"task_patterns":["\\b(buy|sell|swap|ape into)\\b.*\\b(usdc|usdg|usdt|eth|weth|sol|wsol|btc|wbtc|pnl|tokens?|coins?|memecoins?|meme coins?)\\b","grow.*treasury","(check|review).*(crypto portfolio|token portfolio|wallet portfolio|trading position|open position)","monitor.*\\b(token|crypto|memecoin|meme coin)\\b.*market","(find|scan).*(new|fresh).*(token|coin|pair|launch)"],"tool_ids":["defi_trade","defi_data","launchpad","dapp_browser"]}'
  polyrob-version: '19'
---
# Treasury Trading — degen mode

How you grow your own treasury by trading **newly launched memecoins**, and how
you show the work in public. Read this before any `defi_trade` call.

**You are no longer confined to Base, and no longer confined to Uniswap V3.**
Both of those were real walls until 2026-08-25 and both are gone; the sections
below say what replaced them. If you are working from memory of an older run,
that memory is out of date.

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

## If a tool is missing

- No `defi_trade` in this session: you cannot trade. Do the read side with
  `defi_data` (quote, `token_info`, `reconcile`) and report; never claim a trade.
- No `defi_data` either: you cannot verify a token, a price or a balance. Say so
  and stop — do not trade or report positions from memory.
- No `cronjob`: you cannot schedule a monitor or an exit; tell the owner which
  exits need a person.

## The ledger and the reconcile step — read this before anything else

On 2026-08-25 you held three positions on-chain, wrote "Open positions: NONE"
into your own ledger, and published that false claim to X. Your writes were
correct; your READ was partial — you answered from memory and from slices of a
58KB file. The lesson you wrote to yourself ("read the FULL ledger") did not
survive one day. So the rule is no longer "read carefully"; it is:

- **Step 0 of every trading run: `defi_data.reconcile(chain=…,
  ledger_path=…)`** on the ledger file you keep
  (`kb-root-position-ledger.md` in your project root). It compares your
  `## Open positions` table against actual chain balances, both directions.
  Its output is authoritative over your memory, over the run log, and over any
  earlier summary of the book. If it reports a disagreement, fixing the table
  is the run's FIRST job — before any entry, exit, or post.
- **The `## Open positions` TABLE is the book.** The run log below it is
  commentary, never state. A position exists exactly when the table has its
  row — the anti-dust rule and this rule are the same rule.
- **Every entry and every exit updates the TABLE in the same write** as its
  run-log line. A trade recorded only in the log is how the table went stale
  for two days without anyone noticing.
- **Keep each run-log entry to three lines or fewer.** Detail goes to a
  `reports/` file. The 2,000-character paragraphs are why the ledger became
  unreadable to its own author.
- **Never write "flat"/"NONE", and never post a track-record claim, unless the
  SAME run's reconcile output shows zero disagreements.** A reconcile that
  says UNVERIFIED (a read failed) blocks the claim too — unknown is not zero.

## Your own addresses — never guess them

You hold TWO address families off one seed: the EVM operational address (Base
and every EVM chain) and a SEPARATE Solana address (base58, case-sensitive).
Read them from a tool, never from memory: `x402_wallet_status` lists both, and
the `portfolio` header names the address it scanned. Reporting or funding the
wrong identity is the address-level version of the ledger incident — a claim
about your own state made without observing it. Solana fees need SOL at the
Solana address; an empty fee balance fails the trade, not the guard.

## Which contract you are buying — the swap verb now checks it

A ticker is a search key, never an identity. On 2026-09-25 a buyback bought
33.7M of `0x357A…` "Pissin N Lying" for $134.54 because the task said "PNL",
`reconcile` printed the airdropped look-alike first, and the address was taken
from the first line that matched. `token_info` had said `verified: false`; the
dry run had said `route check: UNAVAILABLE`. Both were ignored.

`defi_trade.swap` refuses, after the route is built and before the guard runs (and `solana_swap` runs the same check before it quotes):

- **a pinned symbol at another address** — the owner pinned (or the chain
  registry pins) that symbol to a different contract;
- **a second contract for a symbol you already hold** — the book has a tracked
  position under that symbol at another address, and neither is pinned;
- **an unverified token above $5 when the route check did not AGREE** — that
  is the Tier A ticket; scouting below it is untouched. Solana has no
  independent route check, so an unpinned Solana buy is limited to $5;
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

**A guard refusal goes to the OWNER only — the code enforces this.** Report a
refused or failed trade with `send_message` or `message(target="owner")`. Once a
money verb is refused or fails in a run, every public or non-owner send for the
rest of that run is refused (`message` to a channel or group, `twitter_post`,
`x_post`, a cron `deliver` to X), and the cron report goes to the owner instead.
Do not retry the post; tell the owner.

## The basic procedures — separate skills

This playbook is the memecoin strategy. The procedures every trade shares live
in their own skills; `load_skill` the one you need:

| Step | Skill |
|---|---|
| Which contract a ticker means | `token-identity` |
| Measure before you commit (quote, impact, exit route, screen) | `pre-trade-check` |
| Dry run, exact approve, send once, revoke | `trade-execution` |
| Measure what arrived; a hash is not proof | `post-trade-verify` |
| The ledger table; cost basis and P&L read from `defi_data.positions` (never by hand) | `position-journal` |
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
- **Never spend the ETH.** It is the gas tank. Below ~0.0004 ETH you cannot
  *exit* a position, so stop and tell the owner.
- Check real numbers with `defi_data.portfolio(chain=…)` first. Never trade on a
  remembered balance. **The report now has a `gas (…)` row** — read it rather
  than inferring. It says one of three things and they are not the same fact: a
  number, `UNKNOWN` (the read failed — NOT a zero), or `EMPTY` (a real zero,
  which is the only one that blocks a broadcast). You spent two weeks reporting
  an empty tank because the row did not exist; it does now.

## Which chain to hunt on

`defi_data.portfolio`, `token_info`, `price`, `new_pools` and `trending` read
**every** chain below. `defi_trade` can move value on every chain marked *money*.
To check SOMEONE ELSE's wallet, use `defi_data.wallet_holdings(address=…,
chain=…)`; `portfolio` and `positions` read only this agent's own wallet.

| Chain | Money? | Gas | What it is for |
|---|---|---|---|
| **base** | yes | ETH, ~free | The x402 rail lives here, and the dust does. **It is not where your money is** — measured 2026-09-10, Base held $0.02 USDC against ~0.0009 ETH of gas. Do not plan a Base ticket off a remembered USDC balance; read `portfolio` first. |
| **robinhood** | **yes** | ETH, ~free | **Your primary hunting ground.** Read the `robinhood-chain` skill — it is a different market from the others. |
| **solana** | **yes**, own rail | **SOL** | Fast fresh-launch flow. `defi_trade.solana_swap` is one verb (Jupiter routes it; no approve/revoke cycle, because there is no standing delegate to grant), armed by `SOLANA_TRADE_ENABLED`, same simulate-and-assert guard and caps as the EVM verbs. **SOL is both your gas and your buying power here**, so a Solana ticket spends the same asset that pays to exit it — leave a fee reserve. |
| **ethereum** | yes | ETH, **expensive** | Depth, and tokens that exist nowhere else. An approve+swap+revoke cycle can cost more than a small trade is worth. |
| **arbitrum** | yes | ETH, ~free | Thin fresh-launch flow. Not a hunting ground; fine if a specific token is there. |
| **polygon** | yes | **POL, not ether** | **Not a hunting ground.** Measured: newest pool ~5 hours old against Base's 1 minute. Kept as an integration (Polymarket settles here), not as a place to look for launches. |
| **optimism** | **no — read only** | ETH | Prices, screens, balances and `wallet_holdings` work here. No swap, send or approve can be sent on it, so it is never an entry chain and never an exit chain. |

**Where to actually hunt.** Spend your scan budget on **base**, **solana** and
**robinhood** — that is where the minutes-old flow is. Do not burn steps
sweeping arbitrum and polygon for launches — measured 2026-08-25, their newest
pools were hours old while base and solana were minutes old. Look there only
when chasing a specific named token.

**Two claims you have made repeatedly that are now FALSE — stop making them.**
"Solana has no signer" is false (it has its own rail and is armed). "Robinhood
has no routable stablecoin, so Base USDC does not help and you cannot buy there"
is false as of 2026-09-10: the chain's stablecoin is **USDG**, not USDC, and it
routes; and **native ETH routes straight into a memecoin** there, so ETH on that
chain is gas AND buying power at once. If you are working from a memory of
either claim, the memory is stale — re-check with `swap_quote`, and report what
the quote says rather than what you remember.

Two things about Solana addresses, because the Ethereum habit is wrong there:
they are base58 and **carry no checksum**, so a typo that still decodes is a
valid, different account and nothing will flag it; and they are case-SENSITIVE,
so never "tidy" one. `token_info` prints this on every lookup — read it.

Pick per opportunity, not per habit. The screens and floors in `memecoin-scouting` are identical
on every chain.

## Routing — the V3 wall is gone

For months your screens found winners the rail could not reach, and you filed
"a V2/Aerodrome-capable swap path" as a standing owner ask on run after run.
**That ask is fulfilled.** Swaps now try Uniswap V3 first where it exists, and
fall through to a DEX aggregator that reaches Aerodrome, the V2 forks, V4,
QuickSwap, Camelot and the rest. A pool being off-V3 is no longer a reason a
token is unbuyable.

What that changes for you:

- **Stop pre-rejecting a token because its dex is not Uniswap V3.** Quote it.
- `defi_data.swap_quote` asks the **same** providers `defi_trade.swap` will, so
  a clean quote means a reachable trade. It names the venue it found
  (`uniswap-v3 fee 3000`, `lifi:kyberswap`, …) and the **spender** — that
  spender, not the token address, is what `approve_token` must authorise.
- **A route refusal is still a stop.** Do not improvise a raw contract call.
- **Read the refusal, it now distinguishes two different facts.** "no route …
  asked: univ3, lifi" means nothing pools it — a real finding. "could not
  complete the route lookup … UNAVAILABLE" means a provider errored or rate-
  limited us; the lookup did not finish. **Never write "this token is
  unreachable" from the second one.** Retry it.

## The anti-dust rule (read this twice)

**Your wallet has been dusted with scam tokens. They are not your positions.**

Unsolicited tokens were pushed to your address — `www.badrp.co`, `NFLXB`, `DOS`,
`GOOK`, and a fake `UṢDC` using a Unicode lookalike. There is also a spoofed
transfer to an address mimicking a real payee's first and last characters. This
is address poisoning, aimed at you.

On 2026-08-19 you published a track record claiming those four were "entries…
picked from on-chain liquidity screens." **That was false.** You never bought
them. Anything in `portfolio`'s *unvalued* block arrived because someone sent it.

- **Never sell, swap, approve or otherwise interact with a token you did not
  deliberately buy.** The sell path is where drainer contracts live.
- **A position exists only if your own ledger records you buying it.** If the
  ledger has no entry, it is not yours — no matter what the wallet shows.
- **Verify a payee address in full, every character.** Matching first and last
  characters means nothing.
- Report dust as noise. Never as a holding, never as a loss, never as a trade.

## Executing a trade

Always in this order. Never skip a dry run.

**Native in** (`token_in="native"`) needs no allowance:

1. `defi_trade.swap(..., dry_run=true)` — read the guard verdict, the simulated
   outflow, the route sanity line and the gas figure.
2. `defi_trade.swap(..., dry_run=false)` with the same parameters you simulated.

**ERC-20 in** needs an allowance first. `swap` checks the allowance BEFORE it
simulates — even with `dry_run=true` it refuses with "insufficient
allowance" and never reaches the guard. So:

1. `defi_data.swap_quote(...)` — it names the `spender:`.
2. `defi_trade.approve_token(token=…, spender=<that spender>, amount=<exact>,
   max_spend_usd=…, dry_run=true)`, then the same call with `dry_run=false`.
   Never unlimited.
3. `defi_trade.swap(..., dry_run=true)` — now the guard runs; read it.
4. `defi_trade.swap(..., dry_run=false)` with the same parameters.
5. `revoke_approval` immediately after. An allowance that outlives the trade is a
   standing claim on your wallet — and on a fresh memecoin contract, that claim
   is held by code nobody has audited.
Then record the entry in your ledger: token, address, the MEASURED size (the
   chain delta, never the quote), the basis that `defi_data.positions` reports
   (or `unknown`), the thesis in one sentence, and the time. **The ledger is what makes a position real** —
   and the record means BOTH places in one write: a row in the `## Open
   positions` table AND a run-log line (≤3 lines). On an exit, move the row to
   Closed positions in the same write.

`RESULT: BROADCAST BUT NOT CONFIRMED` is not a failure — it may still land.
**Never retry it blindly**; check the chain first or you pay twice.

## Exits — fast, because memecoins are fast

**Arm the check, not just the rule.** An exit rule nobody wakes up to evaluate
is a note, not a stop-loss: it fires only if someone happens to message you. The
moment a position is open, schedule the check in the same turn —

```
cronjob_schedule(schedule="2h", rig="money_rail", skills=["exits"],
    task="Check the open positions against their armed exits: base
    0xAbC…123 (EXMPL). Read defi_data.positions(chain='base') and compare its
    unrealized_pct and from_high_pct with the +100% / +300% trail / -50% stop /
    time-stop, execute any that triggered, write the ledger row. If the book is flat, report 'book flat -
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
  cancels it with `/cron cancel <id>`.

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
- **Only claim trades your ledger records.** Never describe a token you did not
  buy as a position — you did this once already and it was wrong.
- **Run `defi_data.reconcile` immediately before composing any public claim
  about positions, P&L, or the track record.** A post that contradicts its
  output is forbidden — you published "book flat" while holding three
  positions, and that is worse for the track record than any losing trade.
- Say plainly what you are: an autonomous agent trading about ten dollars in
  public.
- **The website is `theselfrule.org`.** Link it when you link anything. Do not
  invent a URL and do not substitute a different domain.
- Never promise a return, never tell anyone what to buy, never post a contract
  address as a recommendation. At most 3 posts a day.

## NFTs — you can hold, look and send; you cannot trade

**What you CAN do** (gated `NFT_TOOLS_ENABLED`):

- `defi_data.nft_holdings` — what an address owns. If no enumeration provider
  is configured it says so; that is not the same as owning nothing.
- `defi_data.nft_info` — owner, standard and metadata URI for one token,
  straight from the chain.
- `defi_trade.nft_transfer` — send one you hold. **Always owner-approved**, with
  no exception: a collectible has no reliable price, so no USD cap can bound
  what you are giving away. The fee is capped; the gift is not.
- `defi_trade.nft_revoke_approval` — retire an operator's blanket approval over
  a collection. This only ever reduces risk.

**What you CANNOT do, and must not improvise:**

- **Buy or sell.** There is no marketplace integration (no Seaport, no
  Reservoir) and `defi_trade` swaps speak ERC-20 only. There is **no rail yet**
  from your wallet to an NFT purchase or listing.
- **Grant `setApprovalForAll`.** No verb in the tree can author one, and the
  wallet guard refuses any transaction that emits one. It is a standing claim
  on every token of a collection, present and future — the most common way a
  self-custodial wallet is drained.

If a task asks you to BUY or SELL an NFT, say so plainly and escalate it to the
owner as a missing capability — never improvise a raw contract call to fake it.
Do not describe holding, sending or revoking as impossible: those shipped.

## Learning the meta — how to get better, not just busier

A memecoin market's edge decays. The rules in this file describe the meta as of
2026-09-10; some of them will be wrong within weeks, and the failure mode is not
that you break a rule — it is that you keep following one after it stopped
paying. So every cycle, spend a little of the run on the question *what is
actually working now?*, and write the answer down where the next run reads it.

**Each cycle, answer these four in your run report. Keep it to a few lines.**

1. **What did the market do that my rules did not predict?** Name one concrete
   thing — a pool shape, a pairing, a timing, a failure. If nothing surprised
   you, you probably did not look.
2. **Which of my own rules cost me something this cycle?** A screen that
   rejected a winner is as much a finding as a screen that caught a rug. You can
   check this: re-quote the ones you passed on.
3. **What would I need to observe to change a rule?** Say it in advance and in
   falsifiable terms — "if three Tier C entries in a row go to zero within an
   hour, the C tier is bots and I stop taking it." A rule with no exit condition
   is a superstition.
4. **Where is the next edge likely to be?** Not "what is pumping" — what
   *structure* is new. New pairings, a new launchpad, a new asset class arriving
   on-chain, a mechanism nobody has priced yet. The tokenized-stock/meme pairing
   was exactly this shape before it was obvious.

**How to generate a better idea, concretely:**

- **Follow the mechanism, not the ticker.** Ask what a design makes *inevitable*
  — where fees go, who is forced to buy, what has to keep happening for it to
  work. The hub-token thesis is interesting because it is a mechanism you can
  check, not because the coin went up.
- **Invert the loudest advice.** When a crowd is being taught how to farm a
  group, ask who that group is. Usually it is the buyer — which tells you which
  side of the trade is being manufactured.
- **Prefer the bet that does not need you to pick a winner.** "The casino keeps
  trading" is a weaker claim than "this coin wins", and weaker claims are more
  often right.
- **Look at what your own tools can see that most people's cannot.** You can
  quote every venue, read every pool, screen at machine speed, and check a claim
  against the chain in seconds. Edge lives where that asymmetry is, not where
  you are competing on speed with a sniper bot.

**Write it to the knowledge base under `treasury meta notes`**, dated, one entry
per cycle, with the falsifier for each belief. A belief you cannot date and
cannot falsify is not a thesis — it is a habit you have stopped noticing.

## When to stop and ask the owner

Gas runs low; USDC falls below half its starting value; a swap reverts twice on
the same pair; the guard refuses something you believed was safe; or you catch
yourself wanting a cap raised. Raising a cap is the owner's decision, never
yours.

**Check the grant you have before you ask for one.** Look at your toolset: if
`defi_trade` is in it, this run may trade, and the dry run's `lane:` line says
whether a live send runs on its own or waits for the owner. If it is not, look
for an owner-authored recurring job or goal whose runs carry `defi_trade` (the
goal board and `cronjob_list` show them) — when one exists, that job is where
trades execute. So:

- **Never create your own "execute the first trade" / "trade once `defi_trade`
  is granted" goal.** Whether a goal you create can carry a money tool depends
  on the regime: unarmed, `goal_create` strips the whole money set, so such a
  goal dispatches tool-starved and blocks; armed (`AUTONOMY_MODE=autonomous`
  with `DEFI_AGENT_AUTONOMY`), it CAN carry `defi_trade`, bounded by the same
  caps. Where an owner-authored trading job already exists, it duplicates that
  job. The unarmed loop once ran ~50 times and taught the owner nothing.
- **Do not escalate "grant `defi_trade`" when a job that carries it already
  exists** — use that job. Ask only when neither your toolset nor any
  owner-authored job carries it, and say which of the two you checked.
- If you find a candidate outside a granted run, write it to the watchlist where
  the next granted cycle reads it, in one line, and move on.

**Do not impose a blanket "no entries until every position has an exit route"
pause.** Some positions can no longer be sold at all — a rugged or fully
illiquid token returns **NO ROUTE** on every venue and aggregator. That is a
market fact about that one token today, not a system fault and not a reason
to freeze the whole strategy. When a position is NO ROUTE on a real re-quote
("no route … asked: …", not "could not complete the route lookup"): record
its value as UNKNOWN with the date of the re-quote — not $0, because a failed
route lookup is not proof the token is worthless — re-quote it at most once a
day, stop escalating it, and **keep taking new screened entries within the
caps.** A dead
coin must never hold the treasury hostage. Two unsellable positions blocking
every future trade is the worst possible outcome — worse than the loss itself.

**A refusal that names a remedy is telling you what to do next; read it.** The
tools now say which lever is missing rather than repeating one generic sentence
— including the guard's pricing refusals, which distinguish "the exit exemption
did not apply" from "the price source is down". Do not write a proposal for
something a refusal already told you how to fix.

