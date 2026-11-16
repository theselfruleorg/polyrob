# Managing a liquidity position

Read this before you size a deposit, answer "is the pool too large", or propose to
shrink or move a position. Every number comes from a read you made in this turn; a number
you could not read is "not read", never an estimate that looks measured.

## 1. What a position does when the price moves

A position sells the token that rises and buys the token that falls. In a full-range
position the two legs change with the square root of the price:

| Token price against native | Native leg left | Token leg |
|---|---|---|
| −25% | ×0.87 | ×1.15 |
| −50% | ×0.71 | ×1.41 |
| −75% | ×0.50 | ×2.00 |
| +100% | ×1.41 | ×0.71 |

So when a token falls, the position pays its native to the sellers and receives the token.
The native comes back only when buyers return. A position that is most of a pool pays for
most of the net selling.

A concentrated range does the same faster: inside the range the legs change more per price
step, and outside it the position holds ONE asset and earns no fee. A range wholly below
the price holds only native (it buys on the way down); wholly above, only the token (it
sells on the way up).

## 2. The result against holding

This is the question the owner means by "did we lose money".

- value now = native leg + token leg × price + fees owed (both legs)
- held instead = native deposited + token deposited × price (the price NOW)
- result = value now − held instead

The gap before fees is the divergence loss. For a full range it is about 1% for a 25%
price move, 5.7% when the price doubles or halves, and 20% at ×4 or ÷4. Fees are the only thing that pays for
it. Report three numbers, in native: the native leg at deposit and now, the value now, and
the result against holding. Say that the result is not final until a withdrawal.

For a v4 position, `lp_positions(protocol='v4', token_id=…)` prints this: what was deposited
and withdrawn (each at the pool price of its block), the native leg then and now, and the
result against holding. For a v3 position, get the deposit amounts from the deposit result
or the ledger. If a line says NOT READ, say so; do not rebuild it from today's amounts.

## 3. Is the size right for the market

Read the pool (`lp_pool_info`: fee, price, reserves, the last 24 hours) and work out three
ratios.

1. **Depth.** A v4 pool read prints it (what moves the price 2% and 10%). In a full-range
   pool, a swap of `x` native moves the price about `2 × x / native reserve`. Compare with the trades the pool really gets: a typical swap
   that moves the price under 2% is a usable market; over 5% is thin.
2. **Turnover.** 24 h volume ÷ pool value. Under 0.05 the liquidity is idle; 0.2 and above
   is busy. A falling series matters more than one day.
3. **Fee income.** 24 h volume × the LP fee × your share of the in-range liquidity (a v4
   pool read names the largest holders and their share).
   Compare one day of fees with the loss in §2 over the same day.

"Too large" is the wrong word when depth is thin and turnover is high. The usual real
problem is DIRECTION: more native went out than came in. Say which it is.

## 4. When to propose a smaller position

Propose, with the numbers, when two or more hold over a day or longer:

- the net flow is one way (native out is well above native in);
- the volume falls while the price falls;
- one day of fees is far below one day of loss against holding;
- your position is most of the pool.

Then state the trade-off in the same message: withdrawing a part `p` protects `p` of the
native leg, and every later trade moves the price `1 / (1 − p)` times as far. Withdrawing
everything leaves the holders the market that the other positions make. A staged
withdrawal (a part now, a rule for the rest) is usually better than all or nothing.
A withdrawal is the owner's decision when the position gives the token its market.

## 5. Choosing a range

- **Full range**: never out of range, the least fee per unit, the simplest loss profile.
  Right for a new or volatile token.
- **A band around the price**: more fee per unit while the price stays inside, and it must
  be watched; outside the band it earns nothing and holds the falling asset.
- **Only below the price**: native only — a standing bid that buys the token as it falls.
- **Only above the price**: token only — sells into strength, pays no native out.

A range is in ticks of the sorted pair, aligned to the pool's tick spacing. Read the pool
tick first; say the range to the owner as prices as well.

## 6. Moving a position to a new range

A v4 move is one declaration and two guarded transactions.

1. `lp_add(protocol='v4', move_from=<position id>, move_pct=<1..100>, range=…)`. The range
   is `full`, `ticks:lo,hi`, or `low,high` prices in currency1 per currency0. It sends
   nothing: it puts ONE card to the owner with what is withdrawn, the new range, what the
   new range takes and what stays in the wallet. The owner's line is `/lp move`.
2. `lp_add(protocol='v4', seed=<ref>, dry_run=true)` shows both steps.
3. After the owner confirms (one use, one hour): the approvals the dry run names, then the
   same call with `dry_run=false`. It asks the mint first — a refusal the withdrawal cannot
   cure stops the move before anything leaves the position — then withdraws, then mints.

If the mint refuses after the withdrawal, the funds are in the wallet and the result says
so; the same call continues with the mint while the declaration is confirmed. The step is
read from the position's liquidity, so never "repair" a half-done move with separate calls.

A new range needs a different ratio of the two legs. Nothing is swapped: the move mints
what the ratio allows and reports what stays in the wallet. Do not swap to balance the
legs unless the owner asked. To put more into a position without a move, use
`lp_add(protocol='v4', token_id=…)`: it adds in the position's own range; when the fees owed
are not small against the amounts it refuses and names the collect to run first. `LP_ETH_CAP`
is the program's net deposit per position, so a withdrawal makes room only for what the ledger
deposited into THAT position; the mint still meets the wallet's per-transaction limit. The move
checks all of this before it withdraws, and it never sends its withdrawal twice: when a run
stops, read the position and run the same call again.

A v3 move has no single verb: `lp_remove`, then `lp_add` with the new range.

## 7. Watching a position

A position you manage needs a watch that costs nothing per tick. Schedule a read job
(`standing-work` §4): `read_verb={verb: "defi_data.lp_positions", params: {chain,
protocol: "v4", token_id}, alert: [...]}`. The alert compares the read's typed fields:

- `in_range` — `{field: "in_range", equals: "no"}` for a concentrated range;
- `currency0` — the native leg left, `{field: "currency0", below: <floor>}`;
- `value0` — both legs and the fees owed, in currency0;
- `price`, `pool_share_pct`, `currency1`.

Only an alert or a failed read reaches the owner. Set the floor with the owner when the
position is made; an alert is the moment to propose a withdrawal (`propose_action` with
`/lp remove …`) or a move, with the numbers of §2 and §3. It is not a trigger to act alone.

## 8. What to say

Lead with the result in the asset the owner counts in. Then what causes it (direction,
size, range), then the options with the trade-off of each, then your recommendation.
Name every read that failed.
