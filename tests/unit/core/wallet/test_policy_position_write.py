"""PolicyGate.record writes the rail's open-position store (043 A35).

REACH, NOT POLICY: the write is additive, tenant-scoped via the ambient exec
identity, and fully fail-open — the spend is booked exactly as before. These pin:
 - record(positions=[...]) opens the position under the ambient tenant;
 - the spend is STILL booked (audit_log), independent of the position write;
 - no positions -> no store touched (byte-identical to before A35);
 - an anonymous ambient tenant never writes;
 - a crashing store write never breaks the recorded spend.
"""
import os

import pytest

from core.exec_identity import reset_exec_identity, set_exec_identity
from core.open_positions import PositionDelta, get_position
from core.wallet.policy import PolicyGate

MEME = "0xb200000000000000000000ea8625786a776539fb"


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


def _record_buy(gate, **kw):
    gate.record(venue="defi", action="swap", amount_usd=100.0,
                counterparty="0xspender", idempotency_key=None,
                result_ref="0xhash", chain="base",
                positions=[PositionDelta("base", MEME, "MEME", 5000.0, 100.0)],
                **kw)


def test_record_writes_the_position_under_the_ambient_tenant(data_home):
    gate = PolicyGate(max_per_tx_usd=1000.0)
    tok = set_exec_identity("u1", "s1")
    try:
        _record_buy(gate)
    finally:
        reset_exec_identity(tok)
    pos = get_position("u1", "base", MEME,
                       db_path=str(data_home / "open_positions.db"))
    assert pos is not None
    assert pos.qty == 5000.0 and pos.entry_usd == 100.0
    # the spend is booked exactly as before — reach, not policy.
    assert gate.audit_log[-1]["amount_usd"] == 100.0
    assert gate.audit_log[-1]["action"] == "swap"


def test_no_positions_touches_no_store(data_home):
    gate = PolicyGate(max_per_tx_usd=1000.0)
    tok = set_exec_identity("u1", "s1")
    try:
        gate.record(venue="defi", action="approve", amount_usd=0.0,
                    counterparty="0xspender", idempotency_key=None,
                    result_ref="0xhash", chain="base")
    finally:
        reset_exec_identity(tok)
    assert not os.path.isfile(str(data_home / "open_positions.db"))
    assert gate.audit_log[-1]["action"] == "approve"


def test_anonymous_tenant_never_writes(data_home):
    gate = PolicyGate(max_per_tx_usd=1000.0)
    # No ambient identity bound -> ("", "") -> no owner -> no write.
    _record_buy(gate)
    assert not os.path.isfile(str(data_home / "open_positions.db"))
    assert gate.audit_log[-1]["action"] == "swap"     # spend still booked


def test_position_write_failure_is_fail_open(data_home, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr("core.open_positions.apply_deltas", boom)
    gate = PolicyGate(max_per_tx_usd=1000.0)
    tok = set_exec_identity("u1", "s1")
    try:
        _record_buy(gate)                              # must NOT raise
    finally:
        reset_exec_identity(tok)
    # the recorded spend stands even though the bookkeeping write blew up.
    assert gate.audit_log[-1]["amount_usd"] == 100.0
