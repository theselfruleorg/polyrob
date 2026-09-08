"""033 T0.1 — the ambient execution identity, and the wallet_spend repair.

All 114 production `wallet_spend` rows carried an empty user_id, so
`modules/credits/unified_ledger.py::_wallet_leg` matched none of them and
reported $0.00 while claiming metering was on. PolicyGate is a shared singleton
with no execution context; this is its fallback.
"""
import core.event_log as el
from core.exec_identity import (current_exec_identity, reset_exec_identity,
                                set_exec_identity)


def test_default_is_empty_pair():
    assert current_exec_identity() == ("", "")


def test_set_and_reset_round_trip():
    tok = set_exec_identity("u1", "s1")
    try:
        assert current_exec_identity() == ("u1", "s1")
    finally:
        reset_exec_identity(tok)
    assert current_exec_identity() == ("", "")


def test_none_coerces_to_empty_string():
    tok = set_exec_identity(None, None)
    try:
        assert current_exec_identity() == ("", "")
    finally:
        reset_exec_identity(tok)


def test_reset_never_raises_on_a_stale_token():
    tok = set_exec_identity("a", "b")
    reset_exec_identity(tok)
    reset_exec_identity(tok)  # second reset must be a no-op, not an exception


def test_wallet_spend_row_carries_the_ambient_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    el._INSTANCES.clear()
    from core.wallet.factory import _emit_spend_to_event_log

    tok = set_exec_identity("tenant-7", "sess-9")
    try:
        _emit_spend_to_event_log({"venue": "defi", "action": "swap",
                                  "amount_usd": 1.5, "counterparty": None,
                                  "result_ref": "0xabc", "ts": 1.0})
    finally:
        reset_exec_identity(tok)

    rows = el.get_event_log().query(kind="wallet_spend", user_id="tenant-7")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess-9"
    assert rows[0]["attrs"]["amount_usd"] == 1.5
    el._INSTANCES.clear()


def test_unified_ledger_wallet_leg_now_sees_the_spend(tmp_path, monkeypatch):
    """The regression this task exists for: the ledger's tenant-scoped query
    matched 0 of 114 real rows and reported a confident $0.00."""
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "l.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    el._INSTANCES.clear()
    import time

    from core.wallet.factory import _emit_spend_to_event_log
    from modules.credits.unified_ledger import _wallet_leg

    tok = set_exec_identity("tenant-7", "sess-9")
    try:
        _emit_spend_to_event_log({"venue": "defi", "action": "swap",
                                  "amount_usd": 2.25, "counterparty": None,
                                  "result_ref": "0x1", "ts": time.time()})
    finally:
        reset_exec_identity(tok)

    leg = _wallet_leg("tenant-7", 7)
    assert leg["wallet_metering"] == "on"
    assert leg["wallet_payments"] == 1
    assert leg["wallet_spend_usd"] == 2.25
    el._INSTANCES.clear()
