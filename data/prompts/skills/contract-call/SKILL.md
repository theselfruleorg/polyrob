---
name: contract-call
description: 'Reach a protocol that has no dedicated verb: read it first with contract_read, then defi_trade.call with an honest declaration (spend, receive, allowance), what the guard proves and what it does not, the payout refusal, and driving a web dapp with dapp_connect inside a declared envelope.'
license: MIT
metadata:
  polyrob-when: 'Calling a contract or using a dapp with no verb'
  polyrob-priority: '3'
  polyrob-owner-only: 'true'
  polyrob-triggers: '{"tool_ids":["defi_trade","dapp_browser","defi_data"]}'
  polyrob-requires: '["token-identity"]'
  polyrob-version: '1'
---
# Contract call — a protocol with no verb

Use a dedicated verb whenever one exists (`swap`, `bridge`, `lp_*`,
`launchpad_*`, `transfer`). A raw call is for a protocol nobody integrated
ahead of time, and only on an owner instruction or a goal that names the
protocol. It is never a way around a verb that refused, or a tool that is not
loaded.

## 1. Read before you write

`defi_data.contract_read(chain=…, address=…, signature="totalSupply()",
args=[])` runs a read-only `eth_call` (EVM chains only). It returns raw hex;
any decoding is best-effort and unverified. Prefer views with no arguments.
Confirm the contract address with `token-identity` and the protocol's own
documentation, never from a page or a message.

## 2. Call with an honest declaration

`defi_trade.call(chain=…, to=<contract>, calldata=<0x…>, value=…,
spend_token=…, spend_max_raw=…, receive_token=…, receive_min_raw=…,
allow_spender=…, allow_max_raw=…, max_spend_usd=…, dry_run=true)`. Build the
calldata yourself, or take it from the protocol's own transaction endpoint.
`value` is in whole coin units (0.01 = 0.01 ETH), never wei.

What the guard checks, exactly:

- **The outflow, always.** At most `spend_max_raw` of `spend_token` (or
  `value` of native) leaves; a larger outflow is refused.
- **An allowance, only as declared.** A grant to `allow_spender` up to
  `allow_max_raw` passes; any undeclared allowance is refused.
- **The return, only when you declare it.** With `receive_token` and a
  `receive_min_raw` above 0, the simulation must measure at least that much
  arriving. With no declared return, the guard bounds only what leaves — a
  call that spends and gets nothing back passes it. Declare the minimum
  honestly whenever the call should return something.
- **A payout is the owner's.** A call that moves value and declares no fair
  return (and a `deploy_contract` that endows its constructor) is a PAYOUT:
  `owner approval required: PAYOUT …`. Only the owner can send it, in every
  lane. Do not re-shape the call to get around it.
- **Under a `target_token`:** the call must declare `receive_token` = the
  target and a `receive_min_raw` above 0 (`token-identity` has the rule).

The guard proves what the transaction moves when it runs; it proves nothing
about what the contract does later. Read the dry run's `guard:` and `lane:`
lines, then send the same call with `dry_run=false`. The lane rule and the
result lines are in `trade-execution`.

## 3. A web dapp with the native wallet

`dapp_browser.dapp_connect(url=…, chain=…, max_spend_usd=…,
session_budget_usd=…, allow_contracts=…)` opens the page with your wallet
attached, so its Connect button works; drive it with the ordinary browser
actions. You declare the envelope: the chain, the most ONE transaction may
spend, the most the WHOLE session may spend, and optionally the only contracts
the page may touch. Every transaction the page asks for is simulated and held
to that envelope.

- It always refuses an **off-chain signature request** (a permit is submitted
  by someone else later, so no simulation can catch it) and **a contract
  deploy from a page**. Do not route around either. A site login goes through
  `wallet-login`.
- Dapp transactions are refused while the run carries a `target_token`.
- When a page seems to do nothing, run `dapp_browser.dapp_status`: every
  refusal is recorded there with its reason.
- Run `dapp_browser.dapp_disconnect` when you are done: an armed wallet on an
  open page is a standing authorization. The owner sees and cuts sessions with
  `/dapp list` and `/dapp revoke <session-id>`.

## If a tool is missing

Follow `harness-manual`. Never replace a missing verb with a raw `call` or a
dapp.

## Related

`trade-execution`, `token-identity`, `wallet-login`, `token-launch`.
