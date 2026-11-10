"""The wallet submission interlock's states are a declared table (core.state)."""
from core.state.machine import REGISTRY
from core.wallet import submission_journal as sj


def test_the_interlock_table_is_declared_and_well_formed():
    m = REGISTRY["wallet.submission"]
    assert m is sj.SUBMISSION
    assert m.states == {"reserved", "prepared", "booked", "rejected"}
    assert m.validate() == []
    # the unresolved set the interlock refuses on is exactly the non-terminal states
    assert m.states - m.terminal == {"reserved", "prepared"}


def test_its_edges_are_the_guarded_writes():
    m = sj.SUBMISSION
    assert m.allowed("reserved", "prepared")      # bind_signed_hash
    assert m.allowed("reserved", "booked")        # operator_book of a signing row
    assert m.allowed("prepared", "booked")        # mark_booked / operator release
    assert m.allowed("prepared", "rejected")      # unsent x402 / venue rejection
    assert not m.allowed("booked", "prepared")
    assert not m.allowed("rejected", "booked")
