# On-chain operations and trading

This page covers what the agent does on a chain with its own wallet: read-only sight
(`defi_data`), value movement behind the transaction guard (`defi_trade`), the Solana
rail, the book, bridges, NFTs, deploys, contract calls, launchpads, web dapps,
liquidity, agent NFT accounts, the `/trade` mandate, and venue trading in the `markets`
pack. The wallet itself, its caps and the signer are in [wallet.md](wallet.md); the
safety model and the order in which you arm each step are in
[payments.md](payments.md#9-arming-it-one-step-at-a-time).

> ⚠️ **Unaudited — use at your own risk.** These tools move real value on mainnet and
> have had no independent security audit (see
> [SECURITY.md](../../SECURITY.md#crypto--wallet--payment-features)). Everything that
> can move value is OFF by default; only the read-only `defi_data` sight is on.
> [`docs/CONFIGURATION.md`](../CONFIGURATION.md#defi--on-chain-trading) owns every flag
> default.

---

## Sight and spend are separate tools (`tools/defi/`, `core/wallet/tx_guard.py`)

`defi_data` gives the agent eyes on-chain; `defi_trade` gives it a guarded way to move
value; `launchpad` and `dapp_browser` reach two things neither of those covers. They are
split deliberately: **reading is a different risk class from spending**, so they are
different tools behind different flags, and enabling sight never implies enabling spend.

**There is no default network.** The same address is a different token on each network, so
every action names its network: `defi_trade`, `defi_data`, `dapp_browser` and `agent_nft` take
a required `chain=`, the terminal commands a required `--chain`, and a chat command `on base` (or another name).
An action without one is refused with the list of networks; a Solana (base58) address is the
one case read without it.

## `defi_data` — read-only token sight

Gated `DEFI_DATA_ENABLED` (default ON). Read-only actions, **multichain**: the chain registry (`core/wallet/chains.py`)
is the one table of supported chains — Base, Ethereum, Arbitrum, Polygon, Robinhood
Chain, **Optimism** and **BNB Chain** (read only: no money moves there until the owner arms it), and
**Solana** (reads; base58 addresses, no checksum). On BNB, USDC has 18 decimals,
not 6; every balance and amount there is read with the right number. **No signer is
constructed and nothing is broadcast**, so this tool cannot move value:

| Action | What it does | Chains |
|---|---|---|
| `token_resolve` | Ranked *candidate* contract addresses for a ticker — a discovery aid, never a resolver | all, incl. Solana |
| `token_info` | On-chain identity + price + liquidity + safety screen for one contract address | all, incl. Solana |
| `price` | USD price for one contract address | all, incl. Solana |
| `swap_quote` | Prices a swap through the *same* route seam `defi_trade.swap` uses — see what a trade would do for $0 | EVM |
| `portfolio` | The agent's own holdings on one chain, USD-valued, with explicit coverage — includes the wallet's own address in the header and the Uniswap v3/v4 pool deposits it owns, both legs valued | all, incl. Solana |
| `reconcile` | Diffs the chain against the rail's own position book (`open_positions.db`); an unpriced token neither book holds is `unsolicited`, not a disagreement — see [the book](#the-book--reconcile-and-one-verdict-across-every-chain) | all, incl. Solana |
| `token_holders` | Top holders with their share, wallet-vs-contract, LP holders and lock state, creator stake — the concentration read. Solana reads the largest token accounts from the chain (a public RPC often rate-limits this read: it then says NOT CHECKED) | all, incl. Solana |
| `ohlcv` | Price history as candles; pool-scoped, and the pool read is named | all, incl. Solana |
| `new_pools` | Newest-indexed pools (the fresh-launch frontier), unscreened | all, incl. Solana |
| `trending` | Pools an indexer ranks trending, unscreened | all, incl. Solana |
| `scan` | The same pools with a verdict each — SURVIVOR / CANDIDATE / TOO_NEW / PASS / WASH / UNSCREENABLE — from measured depth, flow, age and participation thresholds | all, incl. Solana |
| `nft_holdings` | The NFTs an address holds, own wallet by default — "I could not look" is never an empty list (needs `NFT_TOOLS_ENABLED`) | EVM |
| `nft_info` | One NFT straight from the chain: owner, standard, metadata URI; an unanswered field reads NOT CHECKED (needs `NFT_TOOLS_ENABLED`) | EVM |
| `contract_read` | Raw `eth_call` (returns raw hex, gas- and size-capped) | EVM |
| `wallet_holdings` | ANY wallet's public holdings on one chain — "check this address". A base58 address reads on Solana; a 0x address needs its chain. The agent's own address through it stays an owner read | all, incl. Solana |
| `wallet_activity` | ANY wallet's recent transfers and swaps, with a net-flow summary the verb computes (not a cost-basis P&L) | Solana; EVM via Blockscout, or via `ALCHEMY_API_KEY` where Blockscout has no open API (Robinhood mainnet) or does not answer |
| `token_origin` | Who deployed a token, what else they deployed, whether they still hold it; on Solana the launch buyers and any shared funder ("addresses, not people") | Solana; EVM via Blockscout |
| `positions` | The agent's own open positions: quantity, basis (or "basis unknown"), value, unrealized and realized P&L by average cost, high-water mark — the verb does the arithmetic | all money chains |
| `retire_row` | Closes a record of a holding whose tokens are gone or dust (under $1). It measures the wallet's balance itself, refuses anything else, and stores the measurement as proof. It moves nothing — the one record `defi_data` writes | every money network, incl. Solana |
| `lp_positions` / `lp_pool_info` / `lp_quote` | Pool deposits, a pool's live state, and a deposit quote — see [liquidity](#pool-deposits--liquidity) | EVM |
| `pool_metrics` | Monitors a Pons graduated Uniswap v4 pool: price, in-range liquidity, depth, the impact of one buy, a code-hash check of the pinned contracts; each reading is journaled | Robinhood |

The safety screen in `token_info` merges sources and names each one: on Solana the mint
and freeze authorities, Token-2022 extensions (a permanent delegate is a HARD FAIL),
holders and the pump.fun curve are read from the chain, then Jupiter, RugCheck and GoPlus;
on EVM the proxy slots and `owner()`, Honeypot.is (Ethereum, Base) and GoPlus. A check a
source did not run is listed as NOT CHECKED with the reason. Prices come from up to three
sources (DexScreener, GeckoTerminal, Jupiter); when they disagree the price is DISPUTED
and is never used to size or value a spend. `/check <address> [chain]` or `/check <TICKER>`
runs the same reads for the owner with no model turn.

`token_info` also carries a **survivability** block: will the pool still be there? Each
gate (pool age and depth, liquidity, LP locked or burned, a sell simulation, the sell tax,
mint and freeze authority or owner powers, the top-holder share, bundled supply) reads
`pass`, `fail` or `unknown`. A `fail` anywhere is `REJECT`. The result is the highest
size tier allowed (A, B or C), never a buy signal, and every `unknown` gate lowers that
tier by one step — a gate no source answered is never safe.

The discovery reads (`new_pools`, `trending`, `scan`) say which of three things happened:
`ok` (pools came back), `empty` (the indexer answered with none) or `unavailable` (the
read did not happen). "No pools" and "could not look" are never the same answer.

Two rules run through the whole tier:

**An address is the only identity.** `token_info` / `price` / `contract_read` reject a
ticker outright. `token_resolve` is the single verb that accepts a symbol, and it
returns *every* candidate and never picks — even when there is exactly one match today,
because one match is not a safety property. Binding a symbol to a contract is the
primary injection surface for an agent that reads web pages, and liquidity ranking is
purchasable, so a deeper pool does not mean genuine. **You choose the address.**

**A partial screen is not a clean screen.** The safety screener does not cover every
chain equally — on Robinhood Chain it answers with no honeypot check, no holder data
and empty taxes. `token_info` prints `PARTIAL` and NAMES every check that did not run,
because a check that did not run is not a check that passed, and "no risk flags raised"
over an absent honeypot check is the one failure mode a screen must never have: it
looks exactly like safety. The same rule shapes `scan`, where a pool whose liquidity or
volume the indexer did not report is `UNSCREENABLE` rather than `WASH` — an indexer
that has not caught up is not a manipulator — and `token_holders`, where "no holder
data was reported" is said out loud and is never a wide distribution.

**Unknown is never zero.** An unreadable price renders `unknown` (never `$0.00`); an
unreachable safety screen renders `UNSCREENED` and never contains the word "safe"; a
balance that failed to read is listed separately as unknown rather than as a zero
holding. Only high-confidence prices enter a portfolio total, by one rule for every total —
low-confidence holdings are summed on a `NOT in the total: … indicative ≈ $X` line under it
and unpriceable ones are listed apart, so an attacker-seeded pool cannot inflate the
headline figure you read.

Token metadata (`decimals` above all) is **pinned** from a canonical list for verified
tokens and **frozen on first sight** otherwise; a field the first read could not get is
filled by the first good read, and a later differing read is surfaced as
`metadata_changed` rather than accepted. `decimals` denominates every valuation and, at
`defi_trade`, the delta assertions the security model rests on — a token reporting 6 at
simulation and 18 at execution would break it.

Results are untrusted-wrapped: a token's `name`/`symbol` are chosen by whoever deployed
the contract, so DeFi reads are an injection inlet exactly like a fetched page.
`portfolio` alone is gated while a session is correspondent-tainted (own holdings are
pre-drain reconnaissance); the impersonal verbs stay available.

Without `ALCHEMY_API_KEY` the tool cannot *enumerate* holdings and reports
`coverage: partial`, naming the addresses it scanned and stating outright that a token
outside that set is invisible. The indexer is an upgrade path, never a requirement.

## `defi_trade` — value movement behind a transaction guard

Gated `DEFI_TRADE_ENABLED` (default OFF). **This one can move real funds.** Twenty verbs, every one defaulting `dry_run=true`
so moving funds requires saying so explicitly. Most are behind a second flag of their
own, named in the last column — arming `DEFI_TRADE_ENABLED` alone gives you the six
verbs in the first five rows and nothing else.

| Verb | What it does | Also needs |
|---|---|---|
| `transfer` | Send tokens to an address, bounded by a declared `max_spend_usd` | — |
| `approve_token` | Grant an EXACT allowance to a named spender (unlimited approvals are refused outright) | — |
| `revoke_approval` | Set an allowance back to zero — retire the standing claim | — |
| `swap` | Swap through the route seam: Uniswap V3 from pinned addresses FIRST, an operator-named aggregator only as fallback | — |
| `wrap` / `unwrap` | Native ⟷ the chain registry's pinned wrapped native, 1:1 by the contract's own definition | — |
| `register_agent` | Mint this agent's own ERC-8004 identity on the pinned Identity Registry — see [payments.md](payments.md#61-registering-the-identity-on-chain) | `EIP8004_REGISTER_ENABLED` |
| `set_agent_uri` | Republish that registration file; mints nothing | `EIP8004_REGISTER_ENABLED` |
| `solana_swap` | Swap SPL tokens via Jupiter on Solana — its own mirrored guard stack, see [Solana](#solana_swap--the-solana-rail) | `SOLANA_TRADE_ENABLED` |
| `solana_transfer` | Send SOL (`token='native'`) or an SPL token by mint on Solana. The recipient's token account is created if missing and its rent counts toward `max_spend_usd`; the simulated deltas must match the send exactly, and Token-2022 transfer fees or hooks are refused | `SOLANA_TRADE_ENABLED` |
| `bridge` | Move native value or pinned USDC between chains through Relay, proven by measured arrival — see [bridges](#bridging-between-chains) | `DEFI_BRIDGE_ENABLED` |
| `nft_transfer` | Send one NFT. Always owner-approved — see [NFTs](#nfts) | `NFT_TOOLS_ENABLED` |
| `nft_revoke_approval` | Clear a standing collection approval | `NFT_TOOLS_ENABLED` |
| `deploy_token` / `deploy_contract` | Deploy the pinned fixed-supply token, or your own compiled init code — see [deploys](#deploying-a-contract) | `DEFI_DEPLOY_ENABLED` |
| `solana_deploy_token` | A fixed-supply Token-2022 mint with on-chain metadata | `DEFI_DEPLOY_ENABLED` + `SOLANA_TRADE_ENABLED` |
| `call` | Any contract write, with both directions declared — see [contract calls](#calling-any-contract) | `DEFI_CALL_ENABLED` |
| `lp_add` / `lp_remove` / `lp_collect` | Open or add to, withdraw, and collect fees from a Uniswap pool deposit — see [liquidity](#pool-deposits--liquidity) | `DEFI_LIQUIDITY_ENABLED` |

**The swap route is chosen conservatively.** Uniswap V3 calldata built locally
from pinned contract addresses is the strongest trust position, so it is tried
first wherever a chain has a verified deployment. A third-party aggregator
(currently Li.Fi) is consulted only when local construction finds no pool AND an
operator has named it (`DEFI_ROUTE_AGGREGATOR`, default off) — what makes opaque
aggregator calldata admissible is that the guard never reads calldata: it
simulates and asserts the observed deltas. Every swap additionally passes: a
quote-freshness bound, `amount_out_min > 0`, an allowance precheck, a slippage
bound (`slippage_bps`, default `DEFI_MAX_SLIPPAGE_BPS` = 100), and a
**route-sanity check** — the route's implied price is compared against an
independent source and a disagreement above `DEFI_ROUTE_DRIFT_MAX_PCT` (default
3%, hard ceiling 25%) REFUSES to execute rather than narrating.

**Exits stay executable.** Two mechanisms keep fired stop rules executable
without weakening entries: a sell of a *held* token that no source can price is
valued at the simulation's **measured quote-asset inflow** (the receipt is
exact), and `DEFI_MONITOR_EXITS` (default OFF) lets a forged main-agent turn —
the self-wake monitor loop — run **EXIT-shaped operations only**: revoke,
exit-bounded approve, or a sell of a held token into the chain's quote asset
with the measured inflow asserted post-simulation. Entries, transfers and leaf
turns still refuse; all caps still apply. An approve/revoke records $0 against
the daily cap (the grant is still headroom-checked before landing; value is
recorded once, on the swap), and cap arithmetic runs in cents.

**An unvalued grant is not a free grant.** "Exit-bounded" means
two things, not one: the grant cannot exceed the wallet's own held balance of
the token, **and** its spender is the destination the intent declares *and* a
route spender the chain pins (`univ3_router` / `aggregator_spender`) — the sell
leg approves the router, nobody else. A grant that no source can price is then
charged the **autonomous ceiling** (`DEFI_AUTONOMOUS_MAX_USD`) plus a cent, so
it stays executable but lands on the owner queue and is charged to the caps
instead of passing unattended at $0.00. A grant that is not exit-bounded and
cannot be priced refuses outright, at the verb, naming the router as the
remedy. `revoke_approval` declares no grant and is untouched — clearing a
standing claim on a worthless token must never be blocked.

Every call routes through `core/wallet/tx_guard.py`, the single choke point: **no
value-moving transaction is broadcast without `Decision(allowed=True)`.** The bound is
not "we only wrote safe verbs" — that stops being a security property the moment the
agent supplies its own calldata. It is: **simulate the transaction, measure its observed
asset and allowance deltas, assert them against a declared intent, and refuse on any
disagreement or any probe failure.** Effects are *measured*, never inferred from the
caller's own calldata — checking a declaration against itself is no check at all.

Nine ordered gates, every one fail-closed:

1. **Owner pause** (`polyrob autonomy pause` / `/pause`) — a probe failure counts as paused. Skipped on an owner-direct turn (step 0 below).
2. **Turn origin** — a forged, self-wake, delegation-result, delegated-leaf or
   autonomous turn is refused, and an *unprovable* origin refuses.
3. **Structural** — zero amount, zero/burn destination.
4. **RPC trust** — refuses to arm on the shared public endpoint. Simulation, deltas and
   caps all read from the RPC, so it cannot be the trust anchor for moving money; pin
   `DEFI_EVM_RPC_BASE`.
5. **Simulation trustworthiness** — a revert, an RPC error or an unreadable balance
   refuses. Simulation runs as one `eth_simulateV1` bundle (`[reads, tx, reads]`) so
   state carries between calls and the deltas are real; if the node does not support it,
   the guard refuses rather than falling back.
6. **Delta assertion** — outflow ≤ declared, and a **measured-zero outflow on a declared
   send refuses** (zero is never a cheap transfer; it is an unmeasured one). An
   *undeclared* allowance grant refuses outright — a hidden `approve` is the one effect a
   USD cap cannot bound, because the drain happens in a later transaction the cap never
   sees.
7. **Pricing** — an unpriceable outflow or unknown decimals refuses. No cap can bound a
   number you do not have. A pinned USD stablecoin (a row's `usdc` and `stables`, e.g.
   USDG on Robinhood, USDT on Ethereum — decided by the pinned network and address, never by the token's
   `symbol()`) is valued at its peg while every source is within 2% of $1, and
   the WETH of an ETH-native network that grades below `high` takes Ethereum mainnet's
   ETH price. **A plain send is the exception** (`defi_trade.transfer`,
   `solana_transfer`, `/send`): a wallet sends any token it holds. With no spend-grade
   price the quote says `VALUE NOT VERIFIED` and the owner confirms one extra step
   (`/send … anyway` typed by the owner, or Confirm on the quote — the model cannot
   set it); the send is valued at the weaker
   exit price, else charged the spend-without-asking limit plus a cent, and only the
   owner's own turn runs it — when the agent asks for it, the owner decides.
   **Dollar amounts.** `/send 22 <token> …` sends 22 TOKENS. To send a dollar value,
   write `/send $22 <token> …` (also `$22.50`, `22usd`): the agent converts the dollars at
   the same spend-grade price this step uses, shows the token count and the price on
   the quote, and holds the send to the dollars + 5% (or your `max`). The Confirm
   converts again at the price of that moment. A token with no spend-grade price
   cannot take a dollar amount — the send refuses and asks for a token amount; the
   model never converts by hand. A `/send` that the agent suggests for your decision shows
   the token amount and its USD value, and warns when its reason names a dollar
   figure more than 10x off.
8. **Caps** — the per-tx ceiling, rolling daily cap and replay guard, held under a
   reservation across authorize → broadcast → record so two concurrent transfers cannot
   both clear a nearly-exhausted cap.
9. **Approval lane** — above `DEFI_AUTONOMOUS_MAX_USD` (default `$25`) a call the
   agent makes ON ITS OWN returns `lane=owner_queue` with `allowed=False`. A queue lane
   is not an execute grant: nothing is queued by that result. A LIVE call raises the
   owner's approval card before the tool runs; once the owner approves THAT call, the
   grant rides with it and the guard sends it (`lane=owner_approved`), still held to the
   declared bound and every cap.

**Who is asking (step 0).** Owner intent in a genuine owner turn is authorization. When
the owner asks — in chat, with an owner verb (`/send … go`, `/swap … go`, `/bridge … go`,
`/pay`), or with Confirm on an action card (the same line, run for the owner) —
the pause and the autonomous ceiling do not apply (`lane=owner_direct`); they bound what
the agent does on its own. A model-built spend in the owner's chat above the ceiling is
still tapped once by the owner, because the model wrote the address; a payout-shaped
`call` or an endowed deploy is tapped at ANY value, in the owner's turn too. The per-tx cap,
the daily cap, the simulation and the declared bound bind everyone. A call with no
context (the remote signer, an internal call) is never treated as the owner. Once the
owner has made every decision a run asked for, the agent continues by itself: one wake per
group of decisions, carrying each outcome. A failed Confirm offers its extra check as a
new decision, and Refresh works on an expired quote.

Result rendering is honest by construction: a refusal says **NOT SENT**; an
`owner_queue` lane says it did not execute; a reverted receipt says the transfer did
**not** happen but gas was spent (the row stays in the record, and its amount leaves the
daily limits — gas went and a nonce was consumed); a receipt that never arrived says **BROADCAST BUT NOT CONFIRMED** and
warns against blind retry.

The signing perimeter is deliberately narrow. Transaction signing **refuses a
transaction with no `chainId`** (an unpinned chain is EIP-155 replay exposure across
every EVM chain the address exists on), chain identity is pinned from config and never
read from the RPC, and a preflight verifies the node actually serves the expected chain
before broadcasting. **Typed-data signing is kept off the money path entirely** — a
signed EIP-2612/Permit2 payload is not a transaction, so it would never reach the guard:
no simulation, no delta, no cap, no audit row, and the drain happens later in a
transaction the agent never sees.

`defi_trade` is classified `money` + `high_impact` + `delegate_blocked`, so it is
explicit-grant-only (the agent cannot self-serve it via `load_tool`), never reachable by
a delegated sub-agent, and never in the default toolset. Every spending verb is on the
approval lane — irreversible and self-custodial, with no venue to dispute them. Two
buckets, and a new verb must land in one or the other or a contract test fails:

- **Capped** — swap, solana_swap, approve/revoke, wrap/unwrap, bridge, all three
  deploys, `call`, the NFT revoke, the three LP verbs, and the two ERC-8004 identity
  writes (`register_agent`, `set_agent_uri` — a registration's whole cost is a fee the
  guard prices, so the limit means something). The guard lets a call under
  `DEFI_AUTONOMOUS_MAX_USD` run and sends a larger one to the owner for approval; an
  approved call is sent. A live call also waits for the owner's tap unless
  `DEFI_TIERED_SPEND_LANE` (default OFF) is on: then a call whose *declared* maximum sits
  within the spend-without-asking limit, and a revoke, run and report. A dry run never
  waits. A payout-shaped `call` ([contract calls](#calling-any-contract)) is never exempt.
- **Always owner-approved** — `transfer`, `solana_transfer` and `nft_transfer`. A plain
  send can pay any address, so no flag exempts it; the one exception is a genuine owner
  turn that sends to the owner's own address or the agent's wallet ([wallet.md](wallet.md#the-wallet-corewallet)). An NFT has no reliable
  price, so a USD limit cannot bound it, and a limit that cannot bound what it limits is
  worse than none because it reads as protection.

All of this is ANDed with — never a replacement for — `AGENT_WALLET_MAX_PER_TX_USD`,
`WALLET_DAILY_CAP_USD`, the owner pause and the `owner_queue` lane.

**What the mechanism cannot see**, stated rather than implied: the declaration and the
calldata can share an author (prompt injection authors both, and they will agree — the
turn-origin refusal and the caps are the defence there, not the delta assertion); a
permit signature never reaches the guard at all; and the RPC is the oracle for
everything the guard knows.

## `solana_swap` — the Solana rail

Solana has no allowances, so there is no approve/swap/revoke cycle — one verb,
routed through the **Jupiter** aggregator, with the EVM guard's step order
mirrored against `simulateTransaction` (there is no EVM transaction to hand to
`tx_guard`):

1. `SOLANA_TRADE_ENABLED` (default OFF; independent of `DEFI_TRADE_ENABLED`).
2. Owner pause (the `all` or `trading` scope), fail-closed — skipped on an
   owner-direct turn (the same step 0 as the EVM guard).
3. Turn origin — forged/leaf refusal; `DEFI_AUTONOMOUS_TURN_TRADING` admits
   goal runs, `DEFI_MONITOR_EXITS` admits a sell of a held token into USDC.
4. Mint decimals READ from the chain (`getTokenSupply`) — unreadable refuses;
   a denomination is never guessed (wSOL is 9, not 6).
5. Jupiter's own baked minimum output must CLEAR your `slippage_bps` bound — a
   looser route is refused, never rewritten.
6. Simulation with the **token accounts** (not just the owner) and the
   pre-state, so the deltas are real. A simulation that did not run is not a
   simulation that passed: an empty delta set refuses.
7. The **authority taxonomy** in place of the allowance check: any delegate,
   ownership change, close authority or freeze the simulation reveals refuses
   outright — a swap grants nothing.
8. Rent is classified, not licensed: native SOL outflow beyond plausible rent
   refuses. The simulated outflow of the sold token may not exceed the
   declared amount.
9. Valuation: pinned USDC = $1.00 → high-confidence price → exit-bounded
   fallback → the measured USDC receipt for an unpriceable exit; then the
   declared `max_spend_usd` (in cents), then the **same PolicyGate** — per-tx
   ceiling, rolling daily cap, replay guard — and the `DEFI_AUTONOMOUS_MAX_USD`
   owner-queue lane for a swap the agent starts (an owner-direct turn passes
   it; the per-tx and daily caps still bind). Confirmed swaps land in the same
   spend audit and write the same holdings records (`open_positions.db`) as an
   EVM swap, so `positions`, `reconcile` and `/book` see them.
10. Broadcast requires a pinned `DEFI_SOLANA_RPC` (dry runs work unpinned); the
    signer refuses a foreign fee payer.

⚠️ Solana addresses are **base58 and case-sensitive with no checksum** — a
mistyped address is a valid different account. The wallet derives one Solana
address from the same seed (`m/44'/501'/0'/0'`, Phantom-compatible); see it via
`polyrob wallet`, `x402_wallet_status`, or the `portfolio(chain=solana)` header,
and fund it with SOL for fees before trading.

## The book — `reconcile`, and one verdict across every chain

The treasury figure is **cash flow** (income − spend) and structurally cannot see a
held bag. Two mechanisms close that gap, and neither ever reports a clean book it did
not verify.

`defi_data.reconcile(chain)` is the per-chain comparison, a server-side
check no step budget or partial read can dilute:

- reads the rail's position book (`open_positions.db`), enumerates actual chain
  holdings through the same seams `portfolio` uses, and reports every
  disagreement in both directions — book rows the chain does not back, chain
  holdings the book does not explain, size mismatches;
- a failed read renders `UNVERIFIED`, never zero; quote asset and wrapped
  native are working capital; confidently-priced holdings under $1 classify
  as dust (the same $1 line the records and `retire_row` use);
- it compares only the network it reads: a record that names another network is
  skipped, and a record with no network that reads zero is `unscoped` (UNVERIFIED), not
  missing.

Run it as step 0 of a trading session and again before any public claim about the
records. It reads Solana too (the wallet's SPL token accounts).

**The book across every chain, in one command.** `polyrob wallet book` loops
`portfolio`/`reconcile` over every money-enabled chain plus Solana and prints one
worst-verdict-wins answer — `clean`, `disagreement`, `unverified` or `no_ledger` —
followed by what disagrees. `/book` on Telegram, `/book` in the REPL and the console's
Money page read the same function through the same renderer, so no two seats can
describe one book differently. **A chain it could not read is `unverified`, never
clean.**

The book it compares against is the rail's position store (`open_positions.db`);
an unreadable book is an error, never a clean verdict.

**Closing a record of a holding that is gone.** `/book retire <chain> <address> [go]`
(Telegram and the REPL) and the agent's `defi_data.retire_row(chain, address)` share one path: it
reads the wallet's balance of that token now, refuses anything but zero or dust (under
$1 at a high-confidence price), and stores the measurement as proof. An unreadable
balance or an unpriced non-zero balance refuses — unknown is never zero. A holding that
is worth more is sold, or written off with `/writeoff` ([which tokens the agent trusts](#which-tokens-the-agent-trusts)).

> ⚠️ **The caps are global and chain-blind.** `AGENT_WALLET_MAX_PER_TX_USD`,
> `WALLET_DAILY_CAP_USD` and `DEFI_AUTONOMOUS_MAX_USD` are one number across
> every chain (EVM and Solana share the same rolling daily bucket under
> `venue="defi"`). There are no per-chain caps yet; if you arm a second chain
> with a different ticket size, size the global caps for the riskier one.

## Which tokens the agent trusts

A ticker is a claim any contract can make, so a buy is checked against **which
contract** it names before any quote. A contract is trusted when one of these
covers it — none of which the agent can grant itself:

| Source | What it means |
|---|---|
| canonical | the chain registry's USDC and wrapped native |
| our own launch | the agent's wallet launched or deployed it (proved on-chain) |
| owner target | you wrote the address into this job or goal (`target_token`) |
| owner approved | you tapped *trust* on an identity question, or ran `/wallet trust` |
| owner pin | an older `polyrob wallet pin-token` (the same store) |

An unverified contract is limited to a $5 ticket unless an independent price
check agrees. When two contracts claim one symbol and neither is trusted, or a
larger buy names an unverified contract, the buy is refused and **you are asked
once, in `/pending`**: each contract with its name, where it came from and when
it was first seen, with a *trust it* and a *not trusted* tap. Trusting one
quarantines the held look-alike (it keeps its cost and no longer blocks the
real token); *not trusted* means the agent never buys it and never asks again.

The ticket follows the token at risk. On a **sell** (a token into USDC, a stable or
the wrapped native) that is the token sold: if it is not trusted and no price check
agrees, a sell above $5 needs you. The agent suggests the `/swap` as a decision, and your
Confirm runs it. A `/swap` you type yourself is not held. An exit is never refused
outright, and a sell whose price check DISAGREES is always refused.

From chat (Telegram, the REPL, the console) — no shell needed:

```
/wallet tokens                            # what is trusted, and why; what is quarantined
/wallet trust <chain> <address> [SYMBOL] go
/wallet untrust <chain> <address> go
/writeoff <chain> <address> go [reason]   # a holding's loss = its recorded cost; nothing is sold
/unquarantine <chain> <address> go        # undo a quarantine
```

Each shows what it would do first; `go` records it. The console's Money › Book
shows the same list, the positions the rail tracks, and the same buttons.

---

## Bridging between chains

Gated `DEFI_BRIDGE_ENABLED` (default OFF). `defi_trade.bridge` is the one cross-chain move, routed through **Relay.link** directly.
The owner seats are the CLI and the `/bridge` chat verb:

```bash
polyrob wallet bridge base arbitrum 0.05              # quote + full assertion pass
polyrob wallet bridge base arbitrum 0.05 --execute    # sign and broadcast
polyrob wallet bridge base solana 50 --token-in usdc --token-out usdc   # pinned USDC in and out
polyrob wallet bridges                                # rows not yet proven to arrive
```

In chat: `/bridge <from> <to> <amount> [from usdc] [as <asset>] [go]`.

A bridge runs on **caps, not taps**: it tiers exactly like a swap — under
`DEFI_AUTONOMOUS_MAX_USD` the agent may run one and report, above it the durable owner
queue decides. A delegated sub-agent never bridges, and the spend is recorded to the
same ledger every other verb counts.

**The guard is two-phase**, because arrival is a separate fact from sending:

1. *Before broadcast* — the quote is asserted against what was asked for, the recipient
   is this wallet, the destination chain is in the pinned registry, and the arrival floor
   is positive. The durable row in `bridges.db` is written **before** the send.
2. *After confirmation* — arrival is proved by **measuring the destination balance**
   against `quoted_out − the route's destination slippage`, with the baseline read inside
   the limit reservation just before the broadcast. A provider reporting success over a
   balance that did not move is not believed; an unreadable balance is `unknown`, never
   zero. When Relay returns destination transaction hashes, the delivering transaction
   must also show the inflow to your wallet: repeated hashes count once, a transaction
   older than the bridge does not count, and one proving transaction can prove only one
   bridge row (a hash another row claimed refuses). An EVM route that reports `filled`
   with no hash at all arrives on the balance rise alone, and the row records that basis;
   Solana never falls back to the balance alone.

⚠️ **A bridge that has not arrived by the deadline is `in_flight` — never `failure`.**
A re-sent bridge pays twice, so the row stays an open question handed to you rather than
an error to dismiss. A Relay request id that is already on a row refuses the bridge
before anything is sent, and a `pending` row that never got a transaction is closed as
`never_sent` after an hour, so it stops holding the balance.

The origin is **native** or the origin chain's **pinned USDC** (`token_in="usdc"`; on an
EVM chain the verb approves exactly the amount to the pinned Relay depository and revokes
the approval if the deposit is refused). Any other origin token is refused. The destination
may be native or a token pinned in the chain registry; an arbitrary destination address is
refused. Every direction works, Solana included. Into Solana, the arrival is proven by the
delivering transaction's own balance change for your wallet, so a trade during the wait
never counts as the bridge. Solana → Solana is not a bridge (use a swap or a transfer).

`BRIDGE_WATCHER_ENABLED` (follows `DEFI_BRIDGE_ENABLED`) is the reconciliation ticker.
Every pass re-measures the destination, re-reads the route status, settles rows that
landed, and escalates a still-stuck bridge to you. It is deliberately **not** stopped by
the owner pause: it signs nothing and starts nothing, and an owner who has just stopped
everything is exactly the owner who needs to know where their in-flight funds are.

## NFTs

Gated `NFT_TOOLS_ENABLED` (default OFF). Four verbs: `defi_data.nft_holdings` / `nft_info` (read) and `defi_trade.nft_transfer` /
`nft_revoke_approval` (write).

**There is deliberately no grant verb.** `setApprovalForAll(operator, true)` is refused
by `tx_guard` for every transaction and is not expressible anywhere in the tree — it is
the single approval that hands a whole collection to someone else.

The flag gates the **verbs only**. The guard's refusals for an undeclared ERC-721 or
ERC-1155 outflow, and for any observed `ApprovalForAll` grant, run whether or not it is
set: a security refusal behind a default-off switch is not a refusal.

`nft_transfer` is **always owner-approved** (see [`defi_trade`](#defi_trade--value-movement-behind-a-transaction-guard)): a collectible has no reliable
price, so no USD cap can bound it.
It runs the address-poisoning check ([wallet.md](wallet.md#the-wallet-corewallet)) first. Enumeration needs `ALCHEMY_API_KEY`; without it the
read says so rather than returning an empty list.

## Deploying a contract

Gated `DEFI_DEPLOY_ENABLED` (default OFF). `defi_trade.deploy_token` and `defi_trade.deploy_contract`. A CREATE has no
destination and no counterparty, so `core/wallet/deploy_guard.py` asks different
questions and `tx_guard.authorize` decides on the answers — it stays the ONE
choke point.

What is asserted before anything is signed: it IS a create (an intent claiming a
deploy over a transaction carrying a `to` refuses); the init code is within
EIP-3860 and the runtime within EIP-170; it **produces code at all**
(`eth_simulateV1` returns the deployed runtime as the create's return data — an
empty return is a constructor that reverted quietly); the constructor **moves no
token and grants no allowance** (it is arbitrary code running as the wallet); and
the address is PREDICTED from the nonce so the caller can name it before
broadcast, with the receipt's `contractAddress` re-checked after.

`deploy_token` takes **no bytecode**. It deploys the pinned
`core/wallet/token_template.py` artifact — fixed supply, no mint function, no
owner, no pause, no fee — and the guard compares the produced runtime **byte for
byte**, excluding only the two immutable slots solc recorded. That is what makes
those properties assertable rather than promised.

A deployment sends nothing, so its cost is the FEE: priced as endowment +
worst-case sized gas and charged to the same caps.

```bash
polyrob wallet deploy-token DEMO 1000000000 Demo Coin --chain base   # quote
polyrob wallet deploy-token DEMO 1000000000 Demo Coin --chain base --execute   # deploy
```

**Names and supply.** A token's name and symbol are printable ASCII only (no
accents, no emoji, no look-alike letters from another alphabet) — the same rule for
`deploy_token`, `solana_deploy_token`, `launchpad_launch` and `deploy-token`, because a
name never changes and a look-alike name reads as somebody else's token. The supply
is exact: `1000000000`, `1e9`, `1_000_000` and `1,000,000` all parse (a comma only
between groups of three digits, so `1,5` refuses). A supply with more fractional digits
than the decimals refuses instead of rounding. The quote, the confirm prompt and the
Telegram `/deploy` "add go" line print the exact supply, never `1e+09`, and the
approval text shows the name in quotes so a hidden character is visible. The pinned
bytes rebuild from `core/wallet/contracts/FixedSupplyToken.sol` with solc 0.8.24 (the
command is in the `token_template` docstring).

**No vanity or salt for a token.** `deploy-token` refuses `--vanity` and
`--salt`. The template mints the whole supply to the deployer, and through the
CREATE2 factory the deployer is the factory, not the wallet — the supply would be
stranded for good. A token is always deployed with a plain
CREATE, and the guard asserts the whole supply arriving in the wallet.
`deploy_contract` still accepts a salt, but it refuses a constructor that names
the factory as an owner or admin.

**Solana.** `defi_trade.solana_deploy_token`, gated `DEFI_DEPLOY_ENABLED`
**and** `SOLANA_TRADE_ENABLED`. A mint has no bytecode, so instead of comparing
runtime bytes the guard asserts the whole declared supply arriving in our own
token account, then reads `getAccountInfo(mint)` back after confirmation.

⚠️ **That read-back is the only place the revocation can be seen.** The delta
parser's authority taxonomy reads token ACCOUNT fields and is structurally blind
to `SetAuthority` on a MINT, so "the supply is fixed" is a claim the simulation
cannot make. An unreadable mint renders UNVERIFIED and a surviving authority
renders VERIFIED AND WRONG — neither ever reads as success.

**The name and symbol ARE on-chain**: the mint is created under
Token-2022 with the `MetadataPointer` + `TokenMetadata` extensions, so it carries
them itself — no Metaplex account. Pass `--uri` to a JSON file
(`{name, symbol, description, image}`) for a **logo**; without one the token
shows with no picture anywhere. The metadata update authority is revoked in the
same transaction, so the name and uri are fixed the moment it lands — get the
uri right before `--execute`.

⚠️ A few older AMMs do not support Token-2022. Jupiter and the major venues do.

```bash
polyrob wallet deploy-token DEMO 1e9 Demo Coin --chain solana   # quote
```

## Calling any contract

Gated `DEFI_CALL_ENABLED` (default OFF). `defi_trade.call` executes caller-supplied calldata, so a protocol nobody
integrated ahead of time becomes usable. Declare BOTH directions — at most this
much leaves, **at least this much must come back** (`receive_token` and
`receive_min_raw > 0`) — and the simulation decides whether that held. A call that
declares its inflow (a swap through a router, a claim, a mint that pays the wallet)
keeps the `defi` lane and the spend-without-asking limit.

A call that moves value (native `value > 0` or a `spend_token` outflow) and declares
nothing coming back is **payout-shaped**: it can pay any address, so it goes to the
owner's tap on every lane, an owner turn included, whatever its value. When the target
is the declared token and the selector is `transfer`/`transferFrom`, the guard binds
its destination check to the decoded payee, and the address-poisoning check runs on
that payee and on a payout-shaped target. An `allow_spender` grant follows the same
router rule `approve_token` does. A deploy with `value > 0` is payout-shaped too, and
the guard also asserts that the new contract still holds the whole endowment after its
constructor.

⚠️ The bound `tx_guard` records still applies: delta assertion proves the
transaction does what the intent SAYS, never that the intent is legitimate. Turn
origin, the caps and the owner queue are what bound that.

## Launchpads

Gated `LAUNCHPAD_ENABLED` (default OFF). `launchpad_launch` / `_buy` / `_sell` / `_claim` / `_quote` / `_status`, against
**Pons V2 on Robinhood Chain**. `_claim` collects the creator fees a token you
launched has earned: the launchpad credits your 1% creator tax to a fee escrow that
holds it until you ask, one claim collects what every token this wallet launched has
earned, and it sends nothing — its only cost is gas, and the simulation must prove
the money arrives before anything is broadcast.
Pins are re-verified by code hash on every call and REFUSE on
mismatch; the fee, the config and the economics digest are read live and the
digest is COMMITTED, so terms that move between the quote and the broadcast
revert rather than silently applying. The curve is resolved from the factory's
own record and from nowhere else.

⚠️ The launch snipe tax opens at **99%** and decays over seconds. `launchpad_buy`
refuses above 100 bps rather than paying it; a launch exempts its own recipient.

```bash
polyrob wallet launch DEMO Demo Coin --buy 0.05     # quote the launch + opening buy
polyrob wallet curve 0xToken… --buy 0.1           # price a curve trade
```

## Using a web dapp

Gated `DAPP_BROWSER_ENABLED` (default OFF). `dapp_connect` / `dapp_status` / `dapp_disconnect`. `dapp_connect` injects an
EIP-1193 provider (and the EIP-6963 announcement) into
the session's existing browser context, so a dapp's Connect button works and the
ordinary browser actions keep driving the page. The injected script holds no key
and makes no decision.

`eth_sendTransaction` becomes a `TxIntent` bounded by the envelope declared at
connect time and goes through `tx_guard`; above the autonomous ceiling a call the agent starts waits
on the durable owner queue under a deadline. A page-authored native send to an
address with no code is a payout: it waits for the owner's tap even when that address is
in `allow_contracts`. A page tx that moves a token is undeclared and the guard refuses
it. **A page's off-chain signature requests are refused** — a permit is submitted by someone else later, so no
simulation can catch what it authorizes. A site login goes through `wallet_sign` ([wallet.md](wallet.md#signing-in-to-a-site-wallet_sign)).

⚠️ `dapp_connect` **is** the money verb (the spend happens in a browser callback),
and a SESSION budget is required alongside the per-transaction ceiling: without
it, a per-transaction ceiling is only a per-CLICK ceiling. Run `dapp_disconnect`
when finished — an armed wallet on an open page is a standing authorization.

## Pool deposits — liquidity

`defi_data` reads liquidity without any write flag: `lp_positions` (a wallet's Uniswap v3 or v4
pool deposits, in range or not, with uncollected fees), `lp_pool_info` (a pool's live
state) and `lp_quote` (the liquidity a deposit would mint and the exact paired amounts).
`DEFI_LIQUIDITY_ENABLED=true` arms `defi_trade.lp_add`, `lp_remove` and `lp_collect`; all
default to a dry run. The owner runs the same guarded actions with `polyrob wallet lp`
(`positions`, `pool`, `quote`, `add`, `remove`, `collect`, each with `--chain`) in the
terminal and `/lp` in chat (add `v4` to `positions`, `pool`, `quote`, `remove` or `collect` for Uniswap v4; the terminal command is v3). A short allowance refuses with the exact approval to make.

- **Uniswap v3** on Robinhood, Base, Ethereum, Arbitrum and Polygon: add (a new deposit, or more to an
  existing one), remove (a percentage, optionally burning the empty deposit NFT) and
  collect.
- **Uniswap v4** on Robinhood only (the v4 contracts on Base, Ethereum, Arbitrum and Polygon
  are pinned for reads; pinned, hash-checked before each write; only a pool with no hook, or a pinned one, is accepted — the one pinned hook is Robinhood's): `lp_add(protocol='v4')` opens a new **full-range**
  deposit in a Pons graduated pool. The token leg moves through Permit2: first a standing
  allowance to Permit2, then `approve_token(via='permit2')`, ONE exact grant to the
  pinned v4 manager contract that expires in 15 minutes. A v4 deposit is held to
  `LP_ETH_CAP` (the program total in ETH, default `0` — every v4 deposit refuses until
  the owner sets it), `LP_ETH_DAILY_CAP` and `LP_ETH_FLOOR` (the ETH the wallet keeps).
  `lp_collect(protocol='v4', token_id=…)` takes the fees of a v4 deposit and leaves its
  liquidity unchanged; no approval is needed. With `account=` or `nft=` it collects for a
  deposit that an NFT's account owns, and the fees land in that account. A v4 collect
  declares no minimum (the fees owed are not read first), so a collect that returns nothing
  still costs gas, and the owner's entry pause holds it.
  `lp_remove(protocol='v4', token_id=…)` withdraws a percentage of a v4 deposit with a
  minimum for each side and keeps the deposit NFT (there is no burn on v4).
- **Any v4 pool reads.** A v4 pool has no address; its id is the hash of its two tokens,
  fee, tick spacing and hook. `lp_positions(protocol='v4', token_id=…)` reads one deposit
  from the chain (owner, pool, range, liquidity, what it holds now and the fees it is owed);
  without `token_id` it counts the wallet's deposits on chain, finds their ids and reads each
  one. A pool read shows the fee, the price and the last 24 hours of swaps, volume and fees. `lp_pool_info` and
  `lp_quote` take `token_id`, or the five pool fields (`lp_quote` with `initial_price`
  quotes the first deposit of a pool that has no price yet).
- **A new v4 pool, or a custom range, is your declaration.** `/lp seed <token_a> <token_b>
  <amount_a> <amount_b> price <n> fee <n> spacing <n> [range ticks:lo,hi]` (or the agent's
  `lp_add(protocol='v4', …, tick_spacing=…)`) sends nothing: it puts one card in your chat
  with the pool id, the range, the price, both amounts and the value. Confirm declares it for
  one use within one hour. `/lp seed run <ref> max <usd> go` then creates the pool and mints
  the deposit in ONE transaction, so the pool never stands empty at a price somebody else
  can set; on a pool that exists it mints only while the pool is within your price gap
  (1% by default, 5% at most). Confirm raises no limit: the wallet's caps and `LP_ETH_CAP`
  still judge it. The result reads the deposit and the pool back from the chain.

The guard checks both asset legs, the pinned code and destination, who owns the deposit,
and the mint, hold or burn of its NFT. Collect minimums use the actual read-only collect
returns, because nominal fee growth can round higher. A landed receipt is checked for the
token transfers as well as the deposit events; a native refund cannot be verified from
receipt logs alone.

The console's Money page shows liquidity activity; its recheck lists deposits for the
wallet owner only, and an unread holding is unknown, not zero. Liquidity activity does
not adjust the per-token cost records: the remaining cost basis of a deposit is
unavailable, and the sum of deposits is not that basis. The USD charged for
`lp_collect` is gas, not fee revenue or income.

## Agent NFT accounts

An NFT can have its own wallet: an ERC-6551 token-bound account. Whoever owns the NFT
controls that account. The agent acts through an NFT's account **only by owning the NFT on its
own wallet address** — a person transfers it in, mints it to that address, or the agent mints
it. There are no grants, bindings or operator keys.

`AGENT_NFT_ENABLED` arms `agent_nft` (`agent_nft_snapshot`, `_inspect`, `_journal`,
`_bind_identity`, `_revoke_all`, `_withdraw_token`, `_collection_reveal`,
`_collection_mint`, `_sale_ready`, `_sale_unready`, `_sale_cancel_listing`, `_sale_list`,
`_sale_accept`, `_sale_settle`, `_sale_claim`, `_buy`, `_market_claim`) and the `account=` / `nft=` argument on `defi_trade.transfer`,
`defi_trade.swap`, `defi_trade.lp_collect` (v4), `defi_trade.call`, `launchpad_claim` and
`defi_data.portfolio`. That argument makes the call from the NFT's account: the agent's key
signs and the account holds, through the same `tx_guard` (`TxIntent.via_account`), recorded
against the account. Every other money verb refuses the argument with "this verb does not act
from an NFT account". A call from an account grants no allowance and does not address the
account's own collection, sale or sale rails. Account-admin calls (such as `lock`) refuse, and
every pre-flight read — the pinned code, the lock state (a kind with a lock), that the signing
wallet IS the NFT's owner — fails closed. A refused simulation names the contract's own error
with its arguments (for example `NotListed()` from the sale, or `Refused(target, selector)`
from the account).

Two account kinds are supported, each a standard:

- **Tokenbound AccountV3** — the canonical ERC-6551 account. It has a lock, trusts an
  ERC-2771 forwarder, may hold standing approvals, and accepts the owner's signature as its own.
- **ERC-6551P account** — a draft extension of ERC-6551 that a collection can ship as its own
  account. It has no lock and no forwarder, signs nothing, and refuses standing approvals.
  `/nft trust` pins one only when the collection and the account prove the kind by ERC-165,
  and pins its code by hash.

- **Collections are pinned, none by default.** The owner pins one from chat with
  `/nft trust <collection> on <chain> [go]` (a read-only fact check first): the agent may then
  watch it and act from its accounts, never mint or reveal. Only the system
  administrator's root-owned `/etc/polyrob/agent_nft_collections.json` can grant `mint`
  or `reveal`. A malformed or writable registry refuses as a whole. The file uses the
  collection pin schema `agent-nft-profile/2`; a file in schema 1 still loads.
  `polyrob nft profile upgrade` prints the new form and the difference; `… go` (as root)
  writes it and keeps the old file.
- **A mint pays the collection's pinned sale**, never the collection. A pin with `mint`
  must pin its `sale` (address, code hash, the most one token may cost). The whitelist mint
  takes `allowance` and `proof` and pays the base price exactly; a public mint pays between
  the base price and the pinned maximum, and the sale returns an excess at its settlement.
- **The sale pays the wallet back.** After the sale is settled, `agent_nft_sale_settle` settles
  the wallet's own auction records, and the sale returns what each paid above the clearing
  price. `agent_nft_sale_claim` takes what the sale holds for the wallet (`owed`). Both send
  nothing and cost gas only; the simulation must show the money arrive. `agent_nft_snapshot`
  shows the sale's phase, whether it is settled, and what it owes.
- **Ready for sale.** `agent_nft_sale_ready` makes an agent NFT of an ERC-6551P collection
  ready for sale: its own account calls `readyToSell`, with the journal entry in the same
  transaction. From the next block a plain marketplace fill can move the NFT and everything
  its account holds. Any act through the account ends the readiness, so while the NFT is
  ready the agent refuses its own acts through the account; an act you ask for goes through
  and ends it. `agent_nft_sale_unready` ends the readiness. `agent_nft_sale_cancel_listing`
  withdraws the wallet's orders on the collection's pinned market: one order by its salt, or
  every order, and then revokes the market's approval of the NFTs whose orders it ended.
  Your verbs: `/nft ready <id|collection#id> on <chain> [go]` and
  `/nft unready <id|collection#id> on <chain> [go]`.
  The holdings watch says when an NFT becomes ready, when its readiness ends, and when an NFT
  leaves while it is ready (with the transaction; the price only when the pinned market
  recorded the sale).
- **Buy from a pinned market.** `agent_nft_buy` buys an agent NFT that its seller listed on
  the collection's pinned market: you give it the listing link the seller shared (an ETH
  price only). The quote shows what the NFT's account holds, what the market will check, and
  what it cannot check (count that as worth 0). The market checks those holdings after the
  transfer, in the same transaction, so a seller who empties the account first gets nothing
  and you pay nothing. The buy names the quote's `conditions_hash` and refuses if the account
  changed since. It is always owner-approved. `agent_nft_market_claim` takes back what the
  market owes the wallet. A Seaport listing, a token price and an offer are not supported.
- **List on a pinned market.** `agent_nft_sale_list` lists an agent NFT the wallet owns on
  its collection's pinned market: you name the price (ETH, or a token on the reviewed list)
  and 1 to 30 days. The wallet signs ONE market listing; core builds every field and the
  seller's promise (what the account holds now) and checks each against the network before the
  key is used. The market first gets the approval of that one NFT (one transaction; never an
  approval of the whole collection). The result is the link a buyer opens. A listing does not
  stop the agent: it ends when the NFT moves, when it is cancelled or when it expires, and an
  act that takes a promised holding below its minimum makes the purchase fail. `/nft` shows
  the open listings; the holdings watch says when one is sold (with the price) or expires.
  A Seaport or OpenSea listing is not supported.
- **Accept an offer.** `agent_nft_sale_accept` accepts a buyer's signed offer (the link the
  buyer shared, priced in a reviewed token) on the pinned market: the approval of that one
  NFT when needed, then the sale. The guard checks that exactly that NFT leaves to the buyer
  and that the price less the royalty arrives. The agent makes no offers of its own.
- **Always owner-approved:** `agent_nft_collection_mint`, `agent_nft_withdraw_token` (the
  owner's `/nft send <id> <to> on <chain>`), `agent_nft_sale_ready`, `agent_nft_sale_list`,
  `agent_nft_sale_accept`, `agent_nft_buy` and
  `agent_nft_bind_identity` (it puts the agent's card on the account's own ERC-8004
  identity: `setAgentURI` on the identity an ERC-6551P NFT has from its mint, or a
  `register()` when there is none; the agent's own registration file then names that
  identity, so the two entries link). There is no
  "adopt" step: an NFT that arrives is the agent's to act from at once, and the tokens its
  account already holds are recorded as inherited positions.
- A holdings watch (every 300 s, no model turn) tells the owner once when an NFT of a
  pinned collection arrives in or leaves the agent's wallet; `/nft` lists them with their
  accounts.
- The collection mint and the picture, holdings and brief live in a separately
  installed agent-NFT package; without it those verbs refuse and name the remedy.
- `AGENT_NFT_REVEAL_MAX_GAS_USD` (default `$1.00`) bounds the gas fee of one reveal.

## The trading mandate (`/trade`)

A trading run belongs to a **mandate**: one owner-confirmed record with an allocation,
the networks, a preset and the breakers. There is one live mandate at a time. A mandate
holds intent, never a limit — the per-transaction and daily limits still bind every trade.

```
/trade start 150 base majors                 # ONE card with every number; Confirm creates it
/trade start budget=<usd> chains=<a,b> preset=<name> ticket=<usd> slots=<n>
/trade status                                # positions, exit coverage, breaker, realized P&L
/trade pause | resume                        # entries off or on; exits keep running
/trade stop                                  # final; a card offers "close all"
/trade release                               # clear a latched breaker (owner only)
```

The same verbs run on Telegram and in the REPL; a group refuses them. The agent can also
propose a mandate (the `trading-mandate` skill ends in the same decision) — only the
owner's Confirm creates one.

- **Code builds the jobs, not the model.** Confirm writes a **hunt** job (its cadence from
  the preset), a daily **review** job, and then an **exit** job for each holding the hunt
  buys. A $0 precheck wakes the model only when an entry is possible and a candidate
  passed the cheap screen, or when a holding is near a barrier. A holding with no exit
  job gets one, and the owner is told; an exit job whose holding was sold is cancelled.
- **An entry needs a plan.** Before a mandate swap quotes anything it must state a typed
  plan: a thesis, a confidence, what invalidates it, and the preset. An exit never needs
  one.
- **Promotion stages.** One entry costs at most the stage's ticket: `probe` ($1), then
  `small` (half the preset's ticket), then `normal` (the full ticket). `normal` needs
  closed trades and realized P&L at `small`. The money check also refuses an entry on a
  network that is not the mandate's, past the allocation at cost, with every slot full,
  past the day's entry count, or into a token in its re-entry cooldown.
- **Breakers stop entries, never exits.** Daily loss, drawdown and AI cost over the
  allocation latch until `/trade release`; consecutive losses start a cooldown that
  expires. A latch pauses the hunt and asks the owner one decision.
- **Presets** hold the numbers. Core ships `majors` (5–10 % of the allocation per ticket,
  up to 3 open) and `dca`; there is no default, so the intake asks. ⚠️ The `dca`
  preset buys only once today; for a plain DCA, ask for one scheduled job instead. An install adds its
  own presets (and its own screen lines, `screen.pool` / `screen.survival`) in
  `trading_presets.json` in its data home; a mandate whose preset is gone refuses entries
  and names that file. The agent may only tighten a number; the owner may tune any on the
  intake decision.

The status snapshot and the daily digest carry a $0 **Trading** block: holdings, exit
coverage (PROTECTED / UNPROTECTED), the breaker state, realized P&L with the close reason
of each trade, and the mandate's AI cost. A part that could not be read is named, never
shown as zero. The jobs run unattended: they reach money only with the unattended steps
of [the arming ladder](payments.md#9-arming-it-one-step-at-a-time) armed, and each run
stops at the operator's step limit (`AUTONOMOUS_MAX_STEPS`, 150) and at `AUTONOMOUS_RUN_BUDGET_USD` ($2.00) of provider spend.

---

## Venue trading — the `markets` pack

The venue tools ship in the `markets` pack (`packs/markets/`). The agent can trade on
Hyperliquid and Polymarket — **but live trading is dry-run by default and
double-gated.** Both are in `DELEGATE_BLOCKED_TOOLS` and the correspondent high-impact
set.

- **Hyperliquid** (perps, Arbitrum) and **Polymarket** (prediction markets, Polygon):
  read actions (orderbook, positions, market data — `polymarket_data` is fully read-only,
  no wallet) work whenever the tool is loaded. **Order placement is validated but
  NOT submitted** unless `polyrob_markets.trade_gate.evaluate_live_trade` passes — which
  requires the master switch `CRYPTO_TRADE_LIVE_ENABLED` **and** the venue switch
  (`HYPERLIQUID_TRADING_ENABLED` / `POLYMARKET_TRADING_ENABLED`), all default OFF, and
  the order value within the per-venue cap (`HYPERLIQUID_TRADE_MAX_USD` /
  `POLYMARKET_TRADE_MAX_USD`, default `$5` each). A blocked order returns a `dry_run`
  result, never a silent submission.
- Hyperliquid order requests must include `max_usd`, the maximum USD notional
  the owner approves. The live price and worst-case slippage must fit that bound.
  Approval cards always show this ceiling and Polymarket's `size_usd` amount.
- The `polymarket`/`hyperliquid` wallet venues never hold a spendable float in the
  hub-and-spoke model — funding those venues for live trading is a deliberate,
  separate operator step.
- ⚠️ **Hyperliquid live trading refuses to arm on the polyrob-wallet
  path**: on that path the derived venue key fully owns the
  account it trades, so the venue's approveAgent withdrawal firewall does not
  exist — with `HYPERLIQUID_TRADING_ENABLED` set, the client refuses with an
  error rather than trading behind a firewall the code does not provide. Delegated
  credentials stored through `approve_agent` are not affected.
  `agent_status` reports the actual signer.
- **Polymarket needs its SDK installed** (`py-clob-client`); without it every
  Polymarket verb errors on import. Install it deliberately or leave the tool
  unloaded — the tool_id is never in a default toolset either way.

> Treat live trading as the least-exercised, highest-risk surface. Keep it off unless you
> are actively testing with funds you can lose.

> ⚠️ **The four order-placement verbs are also `PAYMENT_APPROVAL_TOOLS`, and approval
> fires BEFORE the dry-run decision.** The owner-approval pre-hook gates on ACTION NAME,
> not on whether the order will actually be live — so under the default
> `PAYMENT_APPROVAL_MODE=approve`, even a pure paper-trading setup
> (`CRYPTO_TRADE_LIVE_ENABLED` OFF) requires **one owner approval per dry-run order**. An
> autonomous/forged turn (goal, cron, self-wake) has no one to tap "approve" for it, so it
> **cannot paper-trade at all**; exercise dry-run trading interactively, or call the venue tool directly outside the
> approval-gated action. A Polymarket cancel (`cancel_order`/`cancel_all_orders`) never
> waits on a tap — it moves no funds and Polymarket has no stop orders. A Hyperliquid
> cancel runs without a tap too, unless it would remove a trigger order (a stop-loss or
> take-profit), or the open orders cannot be read — then it asks; `revoke_agent` never
> asks (it only reduces authority). The owner pause (`polyrob autonomy pause` / `/pause`;
> the legacy `AUTONOMY_HALT` facet) freezes them too — during an incident, cancel open orders directly
> at the venue, not through the agent.

---

## See also

- [payments.md](payments.md) — the safety model, getting paid, the ledger and the arming order.
- [wallet.md](wallet.md) — the wallet, its caps and audit, the signer, x402 and `wallet_sign`.
- [owner-controls.md](owner-controls.md) — pausing, approving and the owner inbox.
- [security-model.md](security-model.md) — where the money gates sit in the layered trust model.
- [`docs/CONFIGURATION.md`](../CONFIGURATION.md#defi--on-chain-trading) — every DeFi flag with its default and code anchor.
