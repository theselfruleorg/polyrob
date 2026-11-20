"""F9: a wallet spend made outside a turn (no ambient identity) is the OWNER's.

The agent wallet is the operator's, so a tenantless `wallet_spend` row was
invisible to the owner's tenant-scoped ledger. It now carries the one owner
tenant (`core.instance.resolve_owner_user_id`); a bound turn identity still wins.
"""
import time

import pytest

import core.event_log as el
from core.exec_identity import reset_exec_identity, set_exec_identity


@pytest.fixture
def event_log(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    el._INSTANCES.clear()
    yield el
    el._INSTANCES.clear()


def _spend(amount=1.25):
    from core.wallet.factory import _emit_spend_to_event_log
    _emit_spend_to_event_log({"venue": "x402", "action": "pay", "amount_usd": amount,
                              "counterparty": None, "result_ref": None, "ts": time.time()})


def test_a_spend_outside_a_turn_is_the_owners(event_log):
    _spend()
    rows = event_log.get_event_log().query(kind="wallet_spend", user_id="owner-1")
    assert len(rows) == 1 and rows[0]["attrs"]["amount_usd"] == 1.25


def test_the_owners_ledger_counts_it(event_log):
    from modules.credits.unified_ledger import _wallet_leg
    _spend(2.5)
    leg = _wallet_leg("owner-1", 7)
    assert leg["wallet_payments"] == 1 and leg["wallet_spend_usd"] == 2.5


def test_a_bound_turn_identity_still_wins(event_log):
    tok = set_exec_identity("tenant-7", "sess-9")
    try:
        _spend()
    finally:
        reset_exec_identity(tok)
    log = event_log.get_event_log()
    assert len(log.query(kind="wallet_spend", user_id="tenant-7")) == 1
    assert log.query(kind="wallet_spend", user_id="owner-1") == []
