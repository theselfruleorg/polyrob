"""A portfolio read must not spend a whole rail run pricing dust.

⚠️ The defect, measured in production on 2026-09-23. The SAFETY monitor — the one
money rail still enabled while trading was paused — produced NOTHING at 18:20 and
again at 20:20. Both runs died identically:

    20:21:07  Received 5 tool_calls from LLM
    20:21:08  Action 1/5: defi_data_portfolio  {'chain': 'robinhood'}
              …no activity for 600 seconds…
    20:31:00  Agent stalled: No activity for 601.3 seconds

`safety-monitor-baseline.md` stopped at 16:21 and the rate floor that the buyback
reads from it went stale for hours.

It was NOT a missing timeout — every call in the path is bounded (`fetch_balances`
10 s, chain RPC 8 s, `geckoterminal.TIMEOUT_SEC` 12 s, dexscreener passes one in).
It is that ``portfolio`` prices **one holding per network call** in a loop with no
aggregate budget, and this wallet carries 114 ledger rows plus a documented 40+
"held not in ledger" dust cohort. A few dozen slow-but-legal lookups reach 600
seconds without a single call misbehaving. The stall was arithmetic.

The fix is a wall-clock budget — and the hard part is what it must NOT do. This
file's other lesson (2026-08-25) is already that a DexScreener outage put the
wallet's own USDC under the dust warning and the agent "duly reported its entire
spendable balance as not a position". A budget that silently truncated the list
would be that bug again, worse, because the rail would read a SHORTER portfolio
and could conclude a position was gone. So every holding the budget stops us from
pricing must still be NAMED, with its amount, and said to be unpriced *for this
reason* — never dropped, never called dust.
"""
import pytest

from tools.defi import data_tool


class _Ident:
    def __init__(self, symbol, decimals=18, name=None):
        self.symbol, self.decimals, self.name = symbol, decimals, name


class _Price:
    def __init__(self, price_usd, confidence="high"):
        self.price_usd, self.confidence = price_usd, confidence


def _lines(res):
    return (res.content or "") if hasattr(res, "content") else str(res)


# --- the budget helper, in isolation ------------------------------------------- #

def test_the_budget_has_a_default_and_reads_its_env_flag(monkeypatch):
    monkeypatch.delenv("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", raising=False)
    assert data_tool._pricing_budget_sec() > 0
    monkeypatch.setenv("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", "42.5")
    assert data_tool._pricing_budget_sec() == 42.5


def test_a_non_numeric_flag_falls_back_rather_than_raising(monkeypatch):
    """A typo'd flag must not break the one screen that says what we hold."""
    monkeypatch.setenv("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", "not-a-number")
    assert data_tool._pricing_budget_sec() > 0


def test_zero_or_negative_disables_the_budget(monkeypatch):
    """An operator who wants the old unbounded behaviour can still have it."""
    monkeypatch.setenv("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", "0")
    assert data_tool._budget_spent(started=0.0, now=10**9) is False


def test_the_budget_is_not_spent_before_it_elapses(monkeypatch):
    monkeypatch.setenv("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", "100")
    assert data_tool._budget_spent(started=1000.0, now=1050.0) is False


def test_the_budget_is_spent_once_it_elapses(monkeypatch):
    monkeypatch.setenv("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", "100")
    assert data_tool._budget_spent(started=1000.0, now=1101.0) is True


# --- what the render must say ---------------------------------------------------- #

def test_an_unpriced_holding_is_named_with_its_amount_not_dropped():
    """The whole point. A rail that reads a shorter list concludes a position is
    gone — which is the 2026-08-25 USDC bug with a new cause."""
    out = "\n".join(data_tool._budget_exhausted_lines(
        [("0x" + "a" * 40, 1.5, "PNL"), ("0x" + "b" * 40, 2.0, "PONS")], priced=3, total=5))
    assert "0x" + "a" * 40 in out and "0x" + "b" * 40 in out
    assert "PNL" in out and "PONS" in out
    assert "1.5" in out.replace(",", "") and "2.0" in out.replace(",", "")


def test_it_states_how_many_of_how_many():
    out = "\n".join(data_tool._budget_exhausted_lines(
        [("0x" + "a" * 40, 1.0, "X")], priced=3, total=5))
    assert "3" in out and "5" in out


def test_it_says_the_reason_and_never_labels_a_HOLDING_as_absent():
    """An unpriced-for-time holding is not dust and is not absent.

    Scoped to the lines that NAME an address, deliberately. My first version of
    this test forbade the substring "you hold nothing" anywhere in the output and
    failed on the prose that *prohibits* that reading — the same error as the
    2026-09-23 ticker-collision test, which scanned for "real" and caught its own
    explanation. A prohibition is not a claim. What must never happen is a verdict
    attached to a holding.
    """
    lines = data_tool._budget_exhausted_lines(
        [("0x" + "a" * 40, 1.0, "X")], priced=1, total=9)
    blob = "\n".join(lines).lower()
    assert "budget" in blob                      # the reason is stated
    assert "do not conclude" in blob             # and the misreading is forbidden
    addressed = [ln.lower() for ln in lines if "0x" + "a" * 40 in ln]
    assert len(addressed) == 1
    for claim in ("dust", "zero", "nothing", "absent", "gone", "not held"):
        assert claim not in addressed[0], f"{claim!r} labels a holding: {addressed[0]!r}"


def test_no_unpriced_holdings_renders_nothing():
    assert data_tool._budget_exhausted_lines([], priced=5, total=5) == []


# --- the loop honours it -------------------------------------------------------- #
#
# The first version of this file stopped at the two helpers and said so, on the
# grounds that driving the whole verb needs wallet resolution, chain rows, gas
# lines and the index seam. ⚠️ That gap hid the defect below for a day: the
# helpers were both correct and the budget still could not do its job, because
# the loop spends most of its time BEFORE the check it is guarded by. The verb IS
# drivable — `holder=`, `index_fn=` and `identity_fn=` are constructor seams and
# `_gas_lines` monkeypatches to nothing — so it is driven here.


# --- 2026-09-24: the budget was unreachable in production ----------------------- #
#
# MEASURED on prod, free, after the threading fix deployed at 00:40Z:
#
#   TIMEOUT ENFORCED at 60.0s          <- the controller's guard, now working
#   with DEFI_PORTFOLIO_PRICE_BUDGET_SEC=35, STILL cut at 60 s
#
#   fetch_balances(holder, chain=)  0.6 s, 84 holdings
#   _identity per holding          0.26 s mean  ->  ~22 s for 84 rows
#
# Two separate defects, both of which make the budget unable to render:
#
#   1. `_identity` runs at loop-line 18, BEFORE the budget check at loop-line 28.
#      ~22 s of per-holding network work is therefore unbounded, and a budget
#      that only guards the pricing half cannot bound the loop.
#   2. The default was 120 s — LARGER than the 60 s action timeout the controller
#      applies (`TOOL_TIMEOUTS['default']`). A budget bigger than the deadline it
#      is meant to beat can never render its partial answer: the action is killed
#      first and the caller gets nothing, which is the outcome the budget exists
#      to prevent.

def test_the_default_budget_fits_inside_the_controllers_action_timeout(monkeypatch):
    """A budget longer than the 60 s action timeout can never render.

    The controller wraps every call in `asyncio.wait_for(..., 60)` for `defi_data`.
    If the budget outlives that, the verb is cut with nothing instead of returning
    the partial-but-honest list this whole file is about.
    """
    monkeypatch.delenv("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", raising=False)
    assert data_tool._pricing_budget_sec() < 60.0, (
        "the default budget must leave room to RENDER inside the 60 s action timeout")


def test_an_unpriced_holding_with_no_identity_is_still_named_with_raw_units():
    """Once the budget stops identity resolution too, a skipped row has no symbol
    and no decimal amount. It must still be named — raw units are what is known,
    and 'unknown' is not 'absent'."""
    rows = [("0x" + "c" * 40, None, None, 123456789)]
    lines = data_tool._budget_exhausted_lines(rows, priced=0, total=1)
    blob = "\n".join(lines)
    assert "0x" + "c" * 40 in blob
    assert "123456789" in blob.replace(",", "")
    addressed = [ln.lower() for ln in lines if "0x" + "c" * 40 in ln]
    assert len(addressed) == 1
    for claim in ("dust", "zero", "nothing", "absent", "gone", "not held"):
        assert claim not in addressed[0], f"{claim!r} labels a holding: {addressed[0]!r}"


def test_a_three_element_row_still_renders(monkeypatch):
    """The older shape stays valid — the amount/symbol are known when identity
    resolved before the budget ran out."""
    out = "\n".join(data_tool._budget_exhausted_lines(
        [("0x" + "d" * 40, 2.5, "PNL")], priced=1, total=2))
    assert "PNL" in out and "2.5" in out.replace(",", "")


def _addr(n):
    return "0x" + f"{n:040x}"


@pytest.mark.asyncio
async def test_a_spent_budget_stops_IDENTITY_lookups_too(monkeypatch):
    """The defect this file missed the first time.

    With the budget already spent, the loop must not keep resolving identities —
    that was ~22 s of unbounded network work on prod, in front of the check.
    """
    monkeypatch.setenv("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", "10")
    # A FAKE CLOCK rather than a tiny budget. My first attempt set the budget to
    # 0.0001 s and asserted zero identity lookups; it failed with exactly one,
    # because the first row is reached before the clock has advanced at all —
    # which is correct behaviour, not the defect. The clock makes the intent
    # ("the budget is already spent when the loop starts") exact.
    ticks = iter([0.0] + [1000.0] * 50)
    monkeypatch.setattr(data_tool.time, "monotonic", lambda: next(ticks))
    calls = []

    def _counting_ident(chain, addr):
        calls.append(addr)
        return _Ident("TKN")

    holder = _addr(0xABC)
    tool = data_tool.DefiDataTool(
        holder=holder,
        index_fn=lambda h, chain=None: {_addr(1): 10 ** 18, _addr(2): 2 * 10 ** 18,
                                        _addr(3): 3 * 10 ** 18},
        identity_fn=_counting_ident,
        price_fn=lambda c, a: _Price(1.0),
    )
    tool._gas_lines = lambda *a, **k: []
    res = await tool.portfolio(data_tool.PortfolioParams(chain="base"))
    out = _lines(res) or (res.extracted_content or "") + (res.error or "")

    assert calls == [], (
        f"identity was resolved {len(calls)} time(s) after the budget was spent — "
        "the check still sits AFTER the identity lookup")
    # and every holding is still named
    for n in (1, 2, 3):
        assert _addr(n) in out, f"{_addr(n)} was dropped from the render"


@pytest.mark.asyncio
async def test_a_generous_budget_still_prices_everything(monkeypatch):
    """The guard must not fire when there is time — no behaviour change on a
    small, fast wallet."""
    monkeypatch.setenv("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", "300")
    priced = []
    tool = data_tool.DefiDataTool(
        holder=_addr(0xABC),
        index_fn=lambda h, chain=None: {_addr(1): 10 ** 18, _addr(2): 2 * 10 ** 18},
        identity_fn=lambda c, a: _Ident("TKN"),
        price_fn=lambda c, a: (priced.append(a) or _Price(3.0)),
    )
    tool._gas_lines = lambda *a, **k: []
    res = await tool.portfolio(data_tool.PortfolioParams(chain="base"))
    out = _lines(res) or (res.extracted_content or "") + (res.error or "")
    assert len(priced) == 2, f"expected both holdings priced, got {priced}"
    assert "budget exhausted" not in out.lower()
