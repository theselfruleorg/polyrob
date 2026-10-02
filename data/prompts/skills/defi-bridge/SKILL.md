---
name: defi-bridge
description: 'Moving value between chains with defi_trade.bridge: what it can move, the two-phase guard, in_flight is not failure, and proving arrival by a measured destination balance.'
license: MIT
metadata:
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":["defi_trade_bridge"],"keywords":["cross-chain","cross chain","bridge funds","bridge eth","bridge sol","bridge usdc","defi_trade.bridge","in_flight"],"task_patterns":["\\bbridge\\b.*\\b(eth|sol|usdc|usdg|funds)\\b","\\b(bridge|move|send)\\b.*\\b(to|from|between)\\b.*\\b(solana|robinhood|ethereum|arbitrum|polygon|base chain)\\b"],"tool_ids":["defi_trade"]}'
  polyrob-version: '2'
---
# Bridge — moving value between chains

Part of the `treasury-trading` playbook, split out. `defi_trade.bridge` is the
ONE cross-chain move. It takes no `max_spend_usd`: the amount you bridge is the
declaration, and the caps apply to it.

## Moving value between chains — the bridge

You hunt on three chains and your money is rarely on the one holding the
opportunity. Until 2026-09-11 there was no answer to that and you correctly said
so. **There is now: `defi_trade.bridge`.** If you are working from a memory that
"there is no bridge verb", that memory is stale — it was true, it is not now.

**What it does.** Sends the **NATIVE asset** of the origin chain — SOL on solana,
ETH on an EVM chain — through Relay. **Both origins work**: solana → EVM, and
EVM → EVM (Base → Robinhood included). Measured: SOL → Robinhood ~1s at −0.88%;
Base → Robinhood ~1s at −0.05%.

**Bridging INTO Solana is refused.** The destination must be an EVM chain: the
arrival proof measures an EVM balance, and a Solana destination is not
supported yet. Do not try EVM → solana, and do not tell the owner it works.

The **destination** asset may be native OR a token **pinned in the chain
registry** — `weth` / `usdc` where that chain has one:

```
defi_trade.bridge(from_chain="base", to_chain="robinhood", amount=0.036)
defi_trade.bridge(from_chain="base", to_chain="robinhood", amount=0.036,
                  token_out="weth")
    # dry_run defaults TRUE — quotes, asserts and simulates, broadcasts nothing
```

An **arbitrary token address is refused**, and that is not a bug to work around:
every address in the registry was verified on-chain, a supplied one was not. The
ORIGIN is still native-only — an ERC-20 origin needs an approve step whose
spender is a third party's address.

⚠️ **On Robinhood, prefer `native`.** ETH there is gas AND the asset that routes
straight into a position with no allowance, so `token_out="weth"` costs you more
slippage (−0.13% vs −0.05% measured) and buys you nothing unless WETH is wanted
for its own sake.

**CAPS, NOT TAPS** — the owner decided this on 2026-09-12, on purpose. If you
are working from a memory that a bridge is "always owner-approved, never on an
autonomous turn", that memory is stale. The bridge is bounded by the caps, not
by a tap on every move:

- under `DEFI_AUTONOMOUS_MAX_USD` you run it and report — no tap;
- above it, the durable owner queue decides — one tap, and it is the verb's own
  ask (it names the recipient, the USD value, the arrival floor and the relay id);
- the owner's own `/bridge … go` is his act: the ceiling and the pause do not
  hold it (the caps do);
- a delegated **sub-agent/leaf still never bridges**. Report back and let the
  parent run it. That is not a size question.

This is NOT the same as `swap`. A `swap` you start on your own sits on the
spend approval lane and waits for the owner's tap even under the ceiling,
unless the owner turned on `DEFI_TIERED_SPEND_LANE`. Read the dry run's `lane:` line for the verb you are
about to use; do not carry one verb's lane over to another.

The same ceilings as every other money verb apply, and the one that bites on a
big move is the **rolling 24h `WALLET_DAILY_CAP_USD`**, not the per-transaction
ceiling. If a bridge refuses on the daily cap, say the NUMBER — how much headroom
is left and when the window rolls — rather than "refused by PolicyGate".

The owner reaches the same rail from chat with `/bridge <from> <to> <amount>`
(the quote is an action card; Confirm executes it). When he asks you to move
value between chains, **that is the answer** — do not tell him it cannot be
done, and do not ask him to grant you a tool. To put a specific bridge to him,
call `propose_action(command="/bridge <from> <to> <amount>", why=…)`: he gets a
card with one Confirm tap. Never write the line out for him to copy, and never
name a `polyrob …` shell command on a chat seat (a terminal REPL may name
`polyrob wallet bridge`, labeled as a terminal command).

**⚠️ The one rule that costs real money if you get it wrong.** A bridge that has
not arrived by the deadline comes back as **`in_flight`** — that is neither
success nor failure.

- **NEVER re-send an `in_flight` bridge.** A re-sent bridge pays twice. The funds
  are in transit; the deadline expiring is a statement about the clock, not about
  the money.
- **NEVER report `in_flight` as failed** — that is what invites the re-send, from
  you or from the owner.
- **NEVER report it as arrived** either. That is a lie about money.
- It is recorded durably and escalated to the owner. Your job is to say plainly:
  sent, not yet confirmed arrived, here is the id. Then stop.
- **A watcher re-checks it every 2 minutes** and settles it when the destination
  balance actually moves, so an `in_flight` row is no longer the end of the
  story. The owner gets that resolution automatically — you do not need to poll
  it, and you must not re-send while waiting.

An UNREADABLE destination balance is **unknown, never zero** — a dead RPC
returning 0 looks exactly like funds that never arrived, and only one of those is
an incident. Arrival is proven by MEASURING the destination balance, so "the
quote said it would arrive" is not arrival.

**When to bridge at all.** Rarely. Gas and slippage make it a real cost, so it is
not a move you make to chase one ticket — bridge when the imbalance is
structural, i.e. your buying power sits on a chain you are not hunting on. You no
longer need the owner in the loop for a small one, but "I am allowed to" is not
"I should".

## If a tool is missing

The trading verbs need `defi_trade` (and `defi_data` for reads) in THIS session.
If they are not loaded, do the read steps you can, write the plan to the ledger or
watchlist, and tell the owner which grant is missing. Never work around a missing
verb with a raw `call`, a browser dapp, or another agent.

## Related

`treasury-trading`, `robinhood-chain`, `stable-cash`.
