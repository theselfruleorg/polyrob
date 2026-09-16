"""045 lane 2: gates that refuse silently now leave a typed, countable trace."""
import json

import pytest


@pytest.fixture
def tele_db(tmp_path, monkeypatch):
    p = tmp_path / "telemetry_events.db"
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(p))
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    import core.event_log as el
    el._INSTANCES.clear()
    yield str(p)
    el._INSTANCES.clear()


def _rows(db_path):
    import sqlite3

    from core.sqlite_util import execute_retry
    try:
        return [dict(r) for r in (execute_retry(
            db_path, "SELECT kind, user_id, attrs FROM telemetry_events", (),
            fetch="all") or [])]
    except sqlite3.OperationalError as e:
        # The event-log schema is created lazily on first write. When the flag
        # is off, record_refusal never writes, so the table (and sometimes the
        # file) never comes into being — that IS "no rows recorded", mirroring
        # tests/unit/core/surfaces/test_access_log.py's identical fixture.
        if "no such table" in str(e).lower():
            return []
        raise


def test_refusal_rides_the_existing_tool_denied_kind(tele_db):
    from core.security.refusals import record_refusal
    record_refusal("correspondent_taint", tool="defi_trade", user_id="u1")
    rows = _rows(tele_db)
    assert rows[0]["kind"] == "tool_denied"
    a = json.loads(rows[0]["attrs"])
    assert a["reason"] == "correspondent_taint"
    assert a["tool"] == "defi_trade"


def test_unknown_reason_is_rejected_at_the_helper(tele_db):
    from core.security.refusals import record_refusal
    with pytest.raises(ValueError):
        record_refusal("made_up_reason", tool="x")


def test_recording_is_fail_open(tele_db, monkeypatch):
    from core.security import refusals

    def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(refusals, "_log", _boom)
    refusals.record_refusal("approval_denied", tool="x")  # must not raise


def test_flag_off_writes_nothing(tele_db, monkeypatch):
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "off")
    from core.security.refusals import record_refusal
    record_refusal("money_gate", tool="swap")
    assert _rows(tele_db) == []


# --- the two silent refusals wired in the 045 fix wave -----------------------

def test_owner_pause_refusal_of_a_money_verb_is_recorded(tele_db):
    """`pause` was already a declared REFUSAL_REASONS slug with no producer:
    tx_guard refused every money verb under the owner's pause in SILENCE."""
    import types

    from core.wallet.tx_guard import TxIntent, authorize
    intent = TxIntent(chain="base", token="0x" + "11" * 20,
                      to="0x" + "22" * 20, amount_raw=1, max_spend_usd=1.0)
    gate = types.SimpleNamespace()
    d = authorize(intent, {"to": "0x" + "22" * 20}, holder="0x" + "33" * 20,
                  gate=gate, halted_fn=lambda: True,
                  tool_self=types.SimpleNamespace(name="defi_trade"),
                  execution_context=types.SimpleNamespace(user_id="u1",
                                                          session_id="s9"))
    assert d.allowed is False
    rows = _rows(tele_db)
    assert rows and rows[0]["kind"] == "tool_denied"
    a = json.loads(rows[0]["attrs"])
    assert a["reason"] == "pause"
    assert a["tool"] == "defi_trade"
    assert rows[0]["user_id"] == "u1"


def test_entry_pause_refusal_is_recorded(tele_db):
    import types

    from core.wallet.tx_guard import TxIntent, authorize
    intent = TxIntent(chain="base", token="0x" + "11" * 20,
                      to="0x" + "22" * 20, amount_raw=1, max_spend_usd=1.0)
    d = authorize(intent, {"to": "0x" + "22" * 20}, holder="0x" + "33" * 20,
                  gate=types.SimpleNamespace(), halted_fn=lambda: False,
                  entry_paused_fn=lambda: True,
                  tool_self=types.SimpleNamespace(name="defi_trade"))
    assert d.allowed is False
    a = json.loads(_rows(tele_db)[0]["attrs"])
    assert a["reason"] == "pause"
    assert "entry_pause" in a["detail"]


@pytest.mark.asyncio
async def test_a_crashing_approval_provider_is_recorded(tele_db):
    """A provider that raises DENIES — and denied silently, which is exactly the
    class lane 2 exists to close."""
    import types

    from tools.controller.approval import make_approval_hook

    class _Boom:
        name = "boom"

        async def request(self, *a, **k):
            raise RuntimeError("provider exploded")

    hook = make_approval_hook(_Boom(), {"defi_trade"})
    ctx = types.SimpleNamespace(user_id="u1", session_id="s9")
    deny = await hook("defi_trade", {}, ctx)
    assert deny, "a crashing provider must deny"
    a = json.loads(_rows(tele_db)[0]["attrs"])
    assert a["reason"] == "approval_denied"
    assert "provider crashed" in a["detail"]
    assert "RuntimeError" in a["detail"]


def test_the_ledger_write_can_never_change_the_guards_refusal(tele_db):
    """⚠️ The recorder sits INSIDE tx_guard's pause try/except, so an attribute
    error while building its detail would be swallowed as "pause probe failed"
    and the owner would read a probe fault instead of the pause. Caught live by
    test_tx_guard_pause_honesty in this same wave. A ledger write must never be
    able to alter an OUTCOME — so the guard's own text survives a stub intent."""
    import types

    from core.wallet.tx_guard import authorize
    d = authorize(types.SimpleNamespace(), {}, holder="0x" + "33" * 20,
                  gate=types.SimpleNamespace(), halted_fn=lambda: True)
    assert d.allowed is False
    assert "probe failed" not in d.reason
    assert "Nothing was broadcast" in d.reason
