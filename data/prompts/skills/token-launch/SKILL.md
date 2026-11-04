---
name: token-launch
description: 'Launching or deploying a token (launchpad, ERC-20, SPL, compiled bytecode), trading on a launchpad curve, claiming its creator fees, the metadata that must be set before a launch, and putting a launch, deploy or claim to the owner as a card.'
license: MIT
metadata:
  polyrob-when: 'Launching or deploying a token, or claiming its fees'
  polyrob-priority: '2'
  polyrob-triggers: '{"tool_ids":["defi_trade","launchpad"]}'
  polyrob-version: '3'
---
# Token Launch — launching, deploying, claiming

Launching and deploying are separate from trading: read this before `launchpad_launch`, `deploy_token`,
`deploy_contract` or `launchpad_claim`. A raw `call` and a web dapp are in
`contract-call`; `wrap`/`unwrap` are in `trade-execution`.

## Launching a token, and deploying one

If a memory of an older run tells you one of these verbs does not exist, check
your toolset before you believe it, then say which of the two is true. Never
tell the owner a rail does not exist from memory alone.

**`launchpad_launch`** launches a token on the launchpad the `launchpad` tool
supports (its description names the launchpad and the chain): token + bonding
curve in one transaction, a fixed supply, graduating to a locked pool at a set
amount of curve liquidity. `dry_run` defaults TRUE and reads the live terms —
supply, graduation threshold, taxes — do that first, always. An opening buy
(`buy_amount`) is how you take the first position; without one, somebody else
does.

**`launchpad_buy` / `launchpad_sell`** trade an existing launchpad token on its curve,
BEFORE graduation. After graduation it is an ordinary Uniswap pool and
`defi_trade.swap` is the verb. `launchpad_status` tells you which, and how close
to graduating it is. `launchpad_quote` prices a trade exactly and costs nothing.

⚠️ **A launch-block snipe tax can open near 99% and decay over seconds.** A buy
in the launch block then loses almost everything. `launchpad_buy` refuses while the live rate is
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

**Put it to the owner as a card** when the owner asked for it or it is the
owner's call: `propose_action(command="/launch <SYMBOL> <name>", why=…)` (add
`logo <url>`, `desc <text>` or `buy <amount>` when you have them), `"/deploy
<SYMBOL> <supply> <name> on <chain>"` or `"/claim <token-address>"`. The card
fetches the live quote; the owner confirms it. The command-line rule is in
`trade-execution`.

## Your launches EARN — and the money does not come to you

A launch routes its creator tax (`creator_tax_bps` on `launchpad_launch`; the
tool states the default and the maximum) to **a fee escrow**, not to the curve
and not to your wallet. It sits there until you claim it. Nothing pushes it.

⚠️ **Fees left in the escrow are real money left unclaimed.** Never probe the
CURVE for a fee balance with guessed functions (`creatorFees()`,
`pendingFees()`, `claimable()`), never read a revert as "no fees", and never
estimate fees from trading volume. The balance is on the escrow.

- `launchpad_status(token)` **prints the claimable balance** and the escrow
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

## Metadata is not optional (read before any launch)

A token with no logo and no description is indistinguishable from an abandoned
one, and that is most of what a buyer sees before anything else. The fields
exist on every rail — using them is your job, not the code's.

- **`launchpad_launch`** takes `logo` (an https:// or ipfs:// image URL),
  `description`, `twitter` and `website`. Fill at least the logo and the
  description. A listing without them sits below every listing that has
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

Follow `harness-manual`; do the read steps you can, and never work around a
missing verb with a raw `call`, a browser dapp or another agent.

## Related

`defi-liquidity`, `token-identity`, `trade-execution`, `contract-call`.
