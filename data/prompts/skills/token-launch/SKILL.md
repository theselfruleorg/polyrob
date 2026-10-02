---
name: token-launch
description: 'Launching or deploying a token, claiming its creator fees, getting gas back, metadata that must be set before a launch, and using a dapp with the native wallet.'
license: MIT
metadata:
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":["launchpad_launch","launchpad_claim","defi_trade_deploy_token","defi_trade_deploy_contract","defi_trade_solana_deploy_token","defi_trade_call","dapp_browser_dapp_connect"],"keywords":["launch a token","launchpad","deploy token","deploy contract","create a token","mint a token","bonding curve","snipe tax","vanity address","spl token","mint authority","claim fees","creator fees","creator tax","fee escrow","gas back","weth to eth","use a dapp","connect wallet"],"task_patterns":["(launch|deploy|create|mint)\\b.*\\b(token|coin|memecoin|erc-?20|spl)\\b"],"tool_ids":["defi_trade","launchpad","dapp_browser"]}'
  polyrob-version: '2'
---
# Token Launch — launching, deploying, claiming, and dapps

Part of the `treasury-trading` playbook, split out. Launching and deploying are
separate from trading: read this before `launchpad_launch`, `deploy_token`,
`deploy_contract`, `launchpad_claim` or `dapp_browser.dapp_connect`.

## Launching a token, deploying one, and using a dapp (2026-09-13)

Nine verbs you did not have last week. If a memory of an older run tells you any
of these does not exist, that memory is out of date — check your toolset, then
say which of the two it is. Telling the owner a shipped rail does not exist has
cost you twice already.

**`launchpad_launch`** launches a token on **Pons V2**, Robinhood Chain's
dominant launchpad: token + bonding curve in one transaction, fixed 1B supply,
graduating to a locked Uniswap pool at 4.2 ETH of curve liquidity. `dry_run`
defaults TRUE and reads the live terms — do that first, always. An opening buy
(`buy_amount`) is how you take the first position; without one, somebody else
does.

**`launchpad_buy` / `launchpad_sell`** trade an existing Pons token on its curve,
BEFORE graduation. After graduation it is an ordinary Uniswap pool and
`defi_trade.swap` is the verb. `launchpad_status` tells you which, and how close
to graduating it is. `launchpad_quote` prices a trade exactly and costs nothing.

⚠️ **The snipe tax opens at 99% and decays over ~3 seconds.** A buy in the launch
block loses almost everything. `launchpad_buy` refuses while the live rate is
above 100 bps rather than paying it — that refusal is correct, and the answer is
to wait a few seconds, not to look for a way around it. Your OWN launches are
exempt (the factory exempts the deployer), so an opening buy is not taxed.

**`defi_trade.deploy_token`** deploys a plain fixed-supply ERC-20 — no
launchpad, no curve, no liquidity. The whole supply lands in your wallet and
there is no mint function, ever. This does **not** make the token tradable. If
the goal is a tradable token, use `launchpad_launch`, not this.

Do not pass `vanity` or `salt` to it — both are refused. Through the CREATE2
factory the constructor's deployer is the factory, so the whole supply would be
minted to the factory and lost.

**`defi_trade.solana_deploy_token`** is the Solana twin: a fixed-supply SPL
token whose mint authority is revoked in the same transaction, with no freeze
authority ever created. Then the mint is READ BACK and the revocation is
reported as observed — so if it says VERIFIED AND WRONG or VERIFICATION
UNAVAILABLE, do not tell anyone the supply is fixed. That read is the only thing
that can see a mint authority; the simulation structurally cannot.

The name and symbol are written **ON-CHAIN** (Token-2022 metadata, no Metaplex
account), and the metadata update authority is revoked in the same transaction —
so the name cannot be swapped later either. Pass a `uri` to a JSON file holding
the logo, or the token shows in wallets with no image.

**`defi_trade.deploy_contract`** deploys COMPILED bytecode you already have —
never Solidity source, nothing here compiles. Use it only when you genuinely
have a contract to deploy: unlike `deploy_token` there is no template to compare
against, so the guard can promise the constructor moved no token and granted no
allowance, and nothing at all about what the contract does afterwards. If what
you want is a token, `deploy_token` is the verb.

**`defi_trade.call`** executes calldata you built yourself, for a protocol
nobody integrated ahead of time. What the guard checks, exactly:

- **the outflow, always:** at most this much leaves (`spend_token` +
  `spend_max_raw`, or `value`); the simulation refuses a larger outflow and any
  allowance you did not declare;
- **the return, only when you declare it:** with `receive_token` and a
  `receive_min_raw` above 0, the simulation must measure at least that much of
  that token arriving, or the call is refused. With no declared receipt, the
  guard bounds only what leaves — a call that spends and gets nothing back
  passes it. Declare `receive_token` / `receive_min_raw` whenever the call
  should return something, and declare the minimum honestly;
- **under a `target_token`:** the call must declare `receive_token` = the
  target and a `receive_min_raw` above 0, and the guard refuses any simulated
  NFT inflow (unless from the target contract) and any other non-canonical
  token inflow. Canonical assets (USDC, the wrapped native) may arrive, and a
  net delta of zero or less is not an acquisition.

**`dapp_connect`** opens a web dapp with your wallet attached, so its Connect
button works and you can drive the page with the ordinary browser actions. You
declare the envelope: the chain, the most ONE transaction may spend, and the most
the WHOLE session may spend. Use it for a protocol with a UI and no API. Two
things it will always refuse, and you should not try to route around either:
**off-chain signature requests** (a permit is submitted by someone else later, so
no simulation can catch what it authorizes) and **deploying a contract from a
page**. When a dapp appears to do nothing, run `dapp_status` — every refusal is
recorded there with its reason, and that is usually the answer. Run
`dapp_disconnect` when you are done: an armed wallet on an open page is a
standing authorization.

## Your launches EARN — and the money does not come to you

A Pons launch routes its creator tax (`creator_tax_bps` on `launchpad_launch`,
100 bps = 1% by default, 1000 at most) to **a fee escrow**, not to the curve and not to your wallet. It sits
there until you claim it. Nothing pushes it.

⚠️ **This stranded real money.** On 2026-09-14 two launches had accrued fees for
days while the agent reported them "unproven": it probed the CURVE for a balance
(`creatorFees()`, `pendingFees()`, `claimable()` — none of which exist), read the
reverts as "no fees", estimated the total from trading volume, and escalated to
the owner. The balance was one hop away on the escrow the whole time.

- `launchpad_status(token)` now **prints the claimable balance** and the escrow
  address. Read that line. It is the only thing that tells you money is waiting.
- `launchpad_claim(token)` takes it. Nothing leaves the wallet — the only cost is
  gas — and the simulation must prove the money arrives before anything is
  broadcast.
- ⚠️ **The escrow credits an ADDRESS, not a token.** One claim collects what
  EVERY token this wallet launched has earned. Naming a token only tells the tool
  which curve to read the escrow address from. There is no second claim to make.
- `creatorTaxBalance()` on the curve is the portion not yet SWEPT into the escrow.
  Status reports it separately when it is non-zero. It is real and it is not
  claimable yet — do not add the two together.
- Claimed fees arrive in the QUOTE asset. On a native-quoted curve that is ETH,
  which is also your gas, so a claim is the cheapest way to refill a gas tank.

Ledger the claim like any other money event: amount, tx, and the escrow balance
before and after.

## Getting your gas back

`wrap` turns native into WETH. `unwrap` turns it back. Both are 1:1 against the
chain registry's pinned wrapped-native, never a supplied address.

Use `unwrap` the moment a trade leaves you holding WETH you do not need — a
wallet that wrapped its gas to trade and then cannot pay for a transaction is
stuck in a way no other verb can undo.

## Metadata is not optional (read before any launch)

A token with no logo and no description is indistinguishable from an abandoned
one, and that is most of what a buyer sees before anything else. The fields
exist on every rail — using them is your job, not the code's.

- **`launchpad_launch`** takes `logo` (an https:// or ipfs:// image URL),
  `description`, `twitter` and `website`. Fill at least the logo and the
  description. A Pons listing without them sits below every listing that has
  them.
- **`defi_trade.solana_deploy_token`** writes `name` and `symbol` ON-CHAIN
  (Token-2022 metadata — no Metaplex account needed) and takes a `uri` pointing
  at a JSON file `{name, symbol, description, image}`. **The `uri` is where the
  logo comes from**; without it the token shows with no picture in every wallet
  and explorer. Any public HTTPS URL works — if you have the `publish` or
  `app_service` rail, host the JSON yourself and use that URL.
- **`defi_trade.deploy_token`** (EVM) puts name and symbol in the contract
  itself. There is no logo field in ERC-20 — logos come from the token lists and
  from DexScreener, which pick them up after there is a pool.

⚠️ On Solana the metadata update authority is revoked in the SAME transaction,
so the name and uri are fixed forever the moment it lands. Get the uri right
BEFORE you set `dry_run=false` — there is no editing it afterwards.

**Launching is not trading, and it is not free money.** A token you launch has
no holders, no chart and no reason for anyone to buy it. Do it when the owner
asks, or when you have an actual thesis and can say what it is — not to have
done something.

## If a tool is missing

The trading verbs need `defi_trade` (and `defi_data` for reads) in THIS session.
If they are not loaded, do the read steps you can, write the plan to the ledger or
watchlist, and tell the owner which grant is missing. Never work around a missing
verb with a raw `call`, a browser dapp, or another agent.

## Related

`treasury-trading`, `robinhood-chain`.
