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

## 4b. Keeping the position in shape — your job, not the owner's

You maintain the position. On every wake, decide what it needs and put ONE plan to the
owner; the owner only confirms. Do not ask the owner to choose a range: choose it.

1. **Collect** the fees when they are not small against a leg (`propose_action` with
   `/lp collect v4 <id>`).
2. **Re-range (rebalance)** — the default tool. Move the position to a band around the
   price NOW with `lp_add(protocol='v4', move_from=<id>, move_pct=100, range='<low>,<high>')`
   (prices in currency1 per currency0; the tool aligns the ticks). It sends nothing and
   puts one card to the owner. Choose the band:
   - calm or sideways: `low = price / 2`, `high = price × 2` (about 3.4× the depth of a
     full range, the most fee);
   - the token falling fast: `low = price / 3`, `high = price × 3` (about 2.4× the depth;
     the native leg lasts through a further 3× fall);
   - a symmetric band (`low = price / k`, `high = price × k`) takes both legs as they are,
     so nothing is left in the wallet. Prefer it.
   Re-range when the price is outside the band, or in the outer quarter of the band
   (log scale), or the position is still full range. Do not re-range again within 6 hours
   unless the price left the band.
3. **Withdraw** a part (`/lp remove v4 <id> pct <n>`) only when the owner's rule says so,
   or when the pool has no volume left and the flow stays one way.

Say the trade-off in one line on the card: a band earns more fee and gives holders more
depth, and in a continued fall it pays its native out faster than a full range; above the
band it holds only the token and earns nothing until the next re-range.

After the owner confirms the move card: make the approvals the dry run names, then
`lp_add(protocol='v4', seed=<ref>, dry_run=false)`. A move needs `LP_ETH_CAP` above the
native it mints: a refusal names the cap, so tell the owner the value it needs.

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

A position you manage needs a watch that WAKES YOU, not only the owner. Use a job with a
`precheck` (`standing-work` §4), never a `read_verb` job: a read job only sends the owner a
line, and you never decide.

```
cronjob_schedule(schedule="*/15 * * * *", skills=["defi-liquidity"], task="<the rule>",
  precheck={verb: "defi_data.lp_positions",
            params: {chain, protocol: "v4", token_id},
            move_pct: 5, field: "price", max_quiet_hours: 24,
            alert: [{field: "currency0", below: <floor>}]})
```

- A quiet tick costs $0, so tick every 15 minutes: a thin pool can lose half its native
  leg between two 6-hour ticks.
- `move_pct` on `price` wakes you on every 5% move in either direction, measured from the
  last tick that woke you. `price` is currency1 per currency0: when the token falls
  against native, `price` RISES.
- An `alert` bound that stays crossed wakes you on EVERY tick. Use it for a floor you act
  on once, and move or clear it in the same run (`cronjob_edit`).
- A full-range position never leaves its range: an `in_range` alert never fires on it.
- `max_quiet_hours: 24` gives one check-in a day.
- The job carries `defi_data` and `defi_trade` (`tools=[…]`), so that a woken run can
  declare a move (§4b); a declaration sends nothing.
- The task text is the rule a woken run follows (§4b). A condition only in the task text
  is never checked by code: put every number you watch in the `precheck`.
- A read job already on the position: `cronjob_edit(job_id, precheck={…})` turns it into
  this kind. Do not keep both.

When a run wakes: read the `<precheck>` block, apply §4b, declare or propose, and send the
owner one line with the numbers of §2 and §3. Nothing to do: send nothing.

## 8. What to say

Lead with the result in the asset the owner counts in. Then what causes it (direction,
size, range), then the options with the trade-off of each, then your recommendation.
Name every read that failed.
