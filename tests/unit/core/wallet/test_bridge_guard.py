"""Phase 2 is the assertion a single-transaction guard cannot make (037).

`routes/lifi.py` refuses cross-chain because "there is no single transaction
whose deltas can be asserted". These tests pin the answer to that: the MEASURED
destination balance is the proof, and every way the measurement can be absent or
contradicted resolves to something other than a success claim.
"""
from dataclasses import dataclass

import pytest

from core.wallet import bridge_guard as bg


@dataclass
class _Q:
    request_id: str = "0xabc"
    origin_chain_id: int = 792703809
    dest_chain_id: int = 4663
    sender: str = "SOL111"
    recipient: str = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
    currency_in: str = "native"
    currency_out: str = "0x0000000000000000000000000000000000000000"
    amount_in_raw: int = 500_000_000
    min_out_raw: int = 19_000_000_000_000_000
    decimals_out: int = 18


class _Provider:
    def __init__(self, states):
        self._states = list(states)

    def status(self, request_id):
        return self._states.pop(0) if self._states else ("pending", "still pending")


# -- phase 1 ---------------------------------------------------------------

def test_phase1_refuses_a_recipient_that_is_not_us():
    v = bg.assert_phase1(quote=_Q(), expected_recipient="0xsomeoneelse",
                         declared_amount_raw=500_000_000, dest_chain_name="robinhood")
    assert not v.ok and "not this wallet" in v.reason


def test_phase1_refuses_consuming_more_than_declared():
    v = bg.assert_phase1(quote=_Q(), expected_recipient=_Q().recipient,
                         declared_amount_raw=1, dest_chain_name="robinhood")
    assert not v.ok and "more than the declared" in v.reason


def test_phase1_refuses_a_non_positive_arrival_floor():
    v = bg.assert_phase1(quote=_Q(min_out_raw=0), expected_recipient=_Q().recipient,
                         declared_amount_raw=500_000_000, dest_chain_name="robinhood")
    assert not v.ok and "cannot fail is not a guard" in v.reason


def test_phase1_refuses_a_destination_outside_the_pinned_registry():
    """Without a registry row there is no RPC to measure the arrival on, and
    phase 2 would degrade to trusting the provider's status field."""
    v = bg.assert_phase1(quote=_Q(), expected_recipient=_Q().recipient,
                         declared_amount_raw=500_000_000, dest_chain_name=None)
    assert not v.ok and "pinned chain registry" in v.reason


def test_phase1_passes_a_clean_order():
    v = bg.assert_phase1(quote=_Q(), expected_recipient=_Q().recipient,
                         declared_amount_raw=500_000_000, dest_chain_name="robinhood")
    assert v.ok


def test_chain_name_for_id_resolves_robinhood():
    assert bg.chain_name_for_id(4663) == "robinhood"
    assert bg.chain_name_for_id(999999) is None


# -- phase 2 ---------------------------------------------------------------

def _await(provider, balances, **kw):
    seq = list(balances)
    defaults = dict(
        provider=provider, request_id="0xabc", recipient="0xr",
        chain_name="robinhood", min_out_raw=1000, balance_before=0,
        deadline_sec=10, poll_sec=0,
        read_balance=lambda addr, chain: seq.pop(0) if seq else (
            balances[-1] if balances else None),
        sleep=lambda s: None)
    defaults.update(kw)
    return bg.await_arrival(**defaults)


def test_a_measured_arrival_over_the_floor_is_success():
    out = _await(_Provider([("pending", "")]), [5000])
    assert out.state == bg.STATE_ARRIVED
    assert out.measured_delta == 5000


def test_an_arrival_under_the_floor_is_not_success():
    """Short-delivered is not delivered. It parks as in_flight, never 'arrived'."""
    ticks = iter([0.0, 1.0, 999.0])
    out = _await(_Provider([("pending", "a"), ("pending", "b")]), [999],
                 now=lambda: next(ticks))
    assert out.state == bg.STATE_IN_FLIGHT


def test_a_provider_success_with_no_measured_inflow_is_NOT_believed():
    """One of the two is wrong and we do not get to pick which."""
    ticks = iter([0.0, 1.0, 999.0])
    out = _await(_Provider([("success", "filled"), ("success", "filled")]), [0],
                 now=lambda: next(ticks))
    assert out.state == bg.STATE_IN_FLIGHT


def test_a_provider_refund_with_no_inflow_is_a_failure():
    out = _await(_Provider([("failure", "refunded")]), [0])
    assert out.state == bg.STATE_FAILED
    assert "refund" in out.detail.lower() or "failure" in out.detail.lower()


def test_an_unreadable_destination_balance_is_unknown_never_zero():
    """A dead RPC returning nothing must not read as 'funds never arrived'."""
    ticks = iter([0.0, 1.0, 999.0])
    out = _await(_Provider([("pending", "x"), ("pending", "x")]), [None],
                 now=lambda: next(ticks))
    assert out.state == bg.STATE_IN_FLIGHT
    assert "UNKNOWN" in out.detail
    assert out.measured_delta is None


def test_the_in_flight_message_forbids_a_resend():
    """A re-sent bridge pays twice — the text has to say so."""
    ticks = iter([0.0, 1.0, 999.0])
    out = _await(_Provider([("pending", "x"), ("pending", "x")]), [0],
                 now=lambda: next(ticks))
    assert "do NOT re-send" in out.detail or "do NOT re-send" in out.detail


def test_a_deadline_that_passes_ends_the_wait():
    """The loop must terminate even while the provider keeps saying pending."""
    ticks = iter([0.0, 500.0, 500.0])
    out = _await(_Provider([("pending", "x")] * 5), [0], now=lambda: next(ticks))
    assert out.state == bg.STATE_IN_FLIGHT


# -- the durable record ----------------------------------------------------

def test_the_record_is_written_and_listed_before_settlement(tmp_path):
    db = str(tmp_path / "bridges.db")
    bid = bg.record_pending(user_id="rob", quote=_Q(), amount_usd=50.9,
                            balance_before=0, db_path=db)
    rows = bg.open_bridges("rob", db_path=db)
    assert len(rows) == 1 and rows[0]["id"] == bid
    assert rows[0]["state"] == bg.STATE_PENDING


def test_a_settled_bridge_leaves_the_open_list(tmp_path):
    db = str(tmp_path / "bridges.db")
    bid = bg.record_pending(user_id="rob", quote=_Q(), amount_usd=1.0,
                            balance_before=0, db_path=db)
    bg.settle(bid, state=bg.STATE_ARRIVED, detail="ok", db_path=db)
    assert bg.open_bridges("rob", db_path=db) == []


def test_an_in_flight_bridge_STAYS_in_the_open_list(tmp_path):
    """It is the owner's open question; it must not disappear from the list."""
    db = str(tmp_path / "bridges.db")
    bid = bg.record_pending(user_id="rob", quote=_Q(), amount_usd=1.0,
                            balance_before=0, db_path=db)
    bg.settle(bid, state=bg.STATE_IN_FLIGHT, detail="not confirmed", db_path=db)
    rows = bg.open_bridges("rob", db_path=db)
    assert len(rows) == 1 and rows[0]["state"] == bg.STATE_IN_FLIGHT


def test_bridges_are_tenant_scoped(tmp_path):
    db = str(tmp_path / "bridges.db")
    bg.record_pending(user_id="rob", quote=_Q(), amount_usd=1.0,
                      balance_before=0, db_path=db)
    assert bg.open_bridges("someone_else", db_path=db) == []


def test_listing_bridges_never_creates_the_store(tmp_path):
    """A read must not CREATE a db in whatever data home resolved (status-SSOT rule)."""
    import os
    db = str(tmp_path / "bridges.db")
    assert bg.open_bridges("rob", db_path=db) == []
    assert not os.path.exists(db), "a read created the store"
