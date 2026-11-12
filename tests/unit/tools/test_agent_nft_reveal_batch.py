"""The revealer's batch (115 B8): as many due ids as the fee cap lets fit, oldest-due first,
and a run that leaves a backlog says so. The gas figures are the agent-NFT rig's
(tests/integration/test_agent_nft_rig.py test 09): reveal(10) 370,242, reveal(64) 2,238,231."""
import types

import pytest

from core.wallet import collection_reveal as R
from tools.agent_nft import reveal as V
from tools.agent_nft.tool import RevealParams
from tests.unit.tools.test_agent_nft_collection_reveal import (  # noqa: F401 — the fixture
    COLLECTION, Chain, FakeRail, _run, _text, _tool, armed)

#: 4663: base fee ≈0.024 gwei, maxFeePerGas = 2 × base + the rail's 0.1 gwei floor tip.
FEE_4663 = 2 * 24_000_000 + 100_000_000
ETH_USD = 4000.0


def _rig_gas(n):
    """The rig's line: 24,322 fixed + 34,592 per id (370,242 at 10, 2,238,231 at 64)."""
    return 24_322 + 34_592 * n


def _worst_usd(gas, fee=FEE_4663, price=ETH_USD):
    return gas * 1.5 * fee * price / 1e18


# --- the size ---------------------------------------------------------------------

def test_at_4663_prices_the_default_cap_takes_about_30_ids_not_10():
    n, per, priced = V.batch_size(limit=64, probe_ids=64, probe_gas=_rig_gas(64),
                                  max_fee_wei=FEE_4663, unit_price=ETH_USD, max_spend_usd=1.0)
    assert priced and per == pytest.approx(34_592, abs=50)
    assert n == 28
    assert _worst_usd(_rig_gas(n)) <= 0.9 < _worst_usd(_rig_gas(n + 2))   # the guard allows it


def test_the_batch_never_passes_the_guards_per_transaction_maximum():
    n, _per, _p = V.batch_size(limit=R.MAX_REVEAL_IDS, probe_ids=64, probe_gas=_rig_gas(64),
                               max_fee_wei=FEE_4663, unit_price=ETH_USD, max_spend_usd=100.0)
    assert n == R.MAX_REVEAL_IDS == 64


def test_an_explicit_max_ids_bounds_the_batch():
    n, *_ = V.batch_size(limit=5, probe_ids=5, probe_gas=_rig_gas(5), max_fee_wei=FEE_4663,
                         unit_price=ETH_USD, max_spend_usd=1.0)
    assert n == 5


def test_no_price_or_a_zero_fee_leaves_the_limit():
    for price in (None, 0.0, float("nan")):
        n, _per, priced = V.batch_size(limit=64, probe_ids=64, probe_gas=_rig_gas(64),
                                       max_fee_wei=FEE_4663, unit_price=price, max_spend_usd=1.0)
        assert (n, priced) == (64, False)
    assert V.batch_size(limit=7, max_fee_wei=0, unit_price=ETH_USD, max_spend_usd=1.0)[0] == 7


def test_a_cap_too_small_for_one_id_still_names_one_for_the_guard_to_refuse():
    n, *_ = V.batch_size(limit=64, probe_ids=64, probe_gas=_rig_gas(64), max_fee_wei=FEE_4663,
                         unit_price=ETH_USD, max_spend_usd=0.01)
    assert n == 1


def test_without_an_estimate_the_gas_model_stands_in():
    n, per, priced = V.batch_size(limit=64, max_fee_wei=FEE_4663, unit_price=ETH_USD,
                                  max_spend_usd=1.0)
    assert priced and per == V.REVEAL_GAS_PER_ID and n == 28
    assert V.REVEAL_GAS_PER_ID >= 34_592 and V.REVEAL_GAS_FIXED >= 24_322   # rounded UP


def test_a_dearer_chain_takes_fewer():
    cheap = V.batch_size(limit=64, probe_ids=64, probe_gas=_rig_gas(64), max_fee_wei=FEE_4663,
                         unit_price=ETH_USD, max_spend_usd=1.0)[0]
    dear = V.batch_size(limit=64, probe_ids=64, probe_gas=_rig_gas(64), max_fee_wei=FEE_4663 * 2,
                        unit_price=ETH_USD, max_spend_usd=1.0)[0]
    assert dear < cheap and dear == 13


# --- the order and the backlog ------------------------------------------------------

def test_oldest_due_first_then_the_ids_past_their_window():
    head = 1_000
    entries = [(1, 700), (2, 990), (3, 745), (4, 800), (5, 744), (6, 745)]
    # 700 and 744 are past (700+256 <= 1000, 744+256 = 1000): they go last
    assert [i for i, _ in V.oldest_due_first(entries, head)] == [3, 6, 4, 2, 1, 5]


def test_expired_matches_the_contract_window():
    # the transaction lands at head+1 or later; blockhash(rb) needs rb >= block - 256
    assert not V.expired(745, 1_000) and V.expired(744, 1_000)


def test_the_backlog_line_counts_what_remains_and_what_lapses_before_the_next_run():
    head = 1_000
    rest = [(10, 746), (11, 749), (12, 750), (13, 900), (14, 700)]
    line = V.backlog_line(rest, head)
    # 746, 749 end within the next 5 blocks; 750 ends at 1006; 700 is already past
    assert "5 due id(s) remain" in line and "2 pass their 256-block window" in line
    assert "1 already passed it" in line and "WARNING" in line
    assert "WARNING" not in V.backlog_line([(13, 900)], head)
    assert "none" in V.backlog_line([], head)


# --- the verb -----------------------------------------------------------------------

class EstimatingRail(FakeRail):
    estimated = []

    def build_call(self, *, to, data, value=0):
        return {**super().build_call(to=to, data=data, value=value), "maxFeePerGas": FEE_4663}

    def estimate_gas(self, tx):
        ids = R.decode_reveal(tx["data"])
        EstimatingRail.estimated.append(len(ids))
        return _rig_gas(len(ids))


def _armed_tool(chain, seen):
    tool, seen = _tool(chain, seen=seen)
    tool._rail_factory = EstimatingRail
    return tool


def test_explicit_max_ids_is_read_only_when_the_caller_set_it():
    assert V._explicit_max_ids(RevealParams(chain="robinhood")) is None
    assert V._explicit_max_ids(RevealParams(chain="robinhood", max_ids=10)) == 10
    assert V._explicit_max_ids(types.SimpleNamespace(max_ids=7)) == 7


def test_a_backlog_run_takes_the_sized_batch_oldest_due_first(armed):
    head = 1_000
    # 100 due ids: ids 1..20 already past their window, 21..100 inside it, the HIGHER ids older
    blocks = {i: 600 for i in range(1, 21)}
    blocks.update({i: 990 - (i - 21) * 3 for i in range(21, 101)})
    seen = {}
    tool = _armed_tool(Chain(head=head, blocks=blocks), seen)
    res = _run(tool.agent_nft_collection_reveal(RevealParams(chain="robinhood")))
    ids = list(seen["intent"].reveal_ids)
    assert len(ids) == 28 and ids == sorted(ids)
    assert ids == list(range(73, 101))                       # the 28 oldest-due live ids
    assert R.decode_reveal(seen["tx"]["data"]) == ids
    assert EstimatingRail.estimated[-1] == 64                 # sized from the node's estimate of 64
    text = _text(res)
    assert "this run takes 28, oldest-due first" in text and "node estimate" in text
    assert "72 due id(s) remain" in text and "20 already passed it" in text


def test_an_explicit_max_ids_still_bounds_the_verb(armed):
    seen = {}
    tool = _armed_tool(Chain(head=1_000, blocks={i: 900 for i in range(1, 51)}), seen)
    res = _run(tool.agent_nft_collection_reveal(RevealParams(chain="robinhood", max_ids=5)))
    assert list(seen["intent"].reveal_ids) == [1, 2, 3, 4, 5]
    assert "45 due id(s) remain" in _text(res) and "bound 5 (max_ids)" in _text(res)


def test_a_valueless_chain_is_bound_by_the_per_transaction_maximum(armed, monkeypatch):
    monkeypatch.setattr(V, "native_usd", lambda chain, price_fn: 0.0)
    seen = {}
    tool = _armed_tool(Chain(head=1_000, blocks={i: 900 for i in range(1, 101)}), seen)
    res = _run(tool.agent_nft_collection_reveal(RevealParams(chain="robinhood")))
    assert len(seen["intent"].reveal_ids) == 64 and "unpriced or $0" in _text(res)


def test_unmeasured_and_unpriced_falls_back_to_the_old_ten(armed, monkeypatch):
    monkeypatch.setattr(V, "native_usd", lambda chain, price_fn: None)
    seen = {}
    tool, seen = _tool(Chain(head=1_000, blocks={i: 900 for i in range(1, 101)}), seen=seen)
    _run(tool.agent_nft_collection_reveal(RevealParams(chain="robinhood")))
    assert len(seen["intent"].reveal_ids) == V.DEFAULT_MAX_IDS


def test_the_guard_reads_the_same_price_the_batch_is_sized_with():
    """A valueless testnet prices the native coin at exactly 0 — the guard's rule, not 4000."""
    assert V.native_usd("robinhood-testnet", lambda c, a: ETH_USD) == 0.0
    assert V.native_usd("robinhood", lambda c, a: ETH_USD) == ETH_USD
    assert V.native_usd("robinhood", lambda c, a: 0.0) is None


def test_the_batch_is_sized_from_the_guards_own_simulation_first(armed, monkeypatch):
    """The guard prices the simulation's gasUsed; the node's estimate (the least limit that
    succeeds) is higher, so it is only the fallback."""
    from core.wallet import simulation
    probed = []

    def simulate(tx, **kw):
        probed.append(len(R.decode_reveal(tx["data"])))
        return types.SimpleNamespace(gas_used=_rig_gas(len(R.decode_reveal(tx["data"]))))
    monkeypatch.setattr(simulation, "simulate", simulate)
    EstimatingRail.estimated = []
    seen = {}
    tool = _armed_tool(Chain(head=1_000, blocks={i: 900 for i in range(1, 101)}), seen)
    res = _run(tool.agent_nft_collection_reveal(RevealParams(chain="robinhood")))
    assert probed == [64] and EstimatingRail.estimated == []
    assert len(seen["intent"].reveal_ids) == 28 and "(simulation)" in _text(res)
