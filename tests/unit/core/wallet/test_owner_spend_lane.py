"""Owner-authority validation (2026-09-27): an owner spend is booked with its lane.

An owner send during a pause (legal since the owner-direct rule) was booked as
a plain ``wallet_spend`` row, and the status snapshot counted every such row
after the pause as a CRITICAL "PAUSE VIOLATED". The spend now carries the lane
the guard authorized it on, and the alarm skips an owner-direct spend.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

OWNER = "owner-1"
TO = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"
HOLDER = "0x2222222222222222222222222222222222222222"
AMOUNT = 10 ** 18


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda: OWNER)
    monkeypatch.setenv("DEFI_AUTONOMOUS_TURN_TRADING", "true")


def _ctx(**kw):
    base = dict(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                session_id="s-owner", metadata={})
    base.update(kw)
    return SimpleNamespace(**base)


# -- 2. an owner spend is booked with its lane ----------------------------------

def _authorize(ctx, gate, *, forged=False, halted=False, idem="idem-1"):
    intent = tx_guard.TxIntent(chain="base", token=None, to=TO, amount_raw=AMOUNT,
                               max_spend_usd=2500.0, expected_allowance_grants=(),
                               idempotency_key=idem)
    return tx_guard.authorize(
        intent, {"to": TO, "data": "0x", "value": AMOUNT, "chainId": 8453},
        holder=HOLDER, gate=gate, execution_context=ctx,
        simulate_fn=lambda **_: Deltas(ok=True, native_delta=-AMOUNT, token_deltas={},
                                       allowance_deltas={}, gas_used=21_000),
        price_fn=lambda chain, addr: 2000.0, fallback_price_fn=None,
        rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: halted, entry_paused_fn=lambda: False,
        forged_fn=(lambda c, t: forged), autonomous_ok_fn=(lambda c, t: forged))


def _record(gate, idem="idem-1"):
    gate.record(venue="defi", action="transfer", amount_usd=2000.0, counterparty=TO,
                idempotency_key=idem, result_ref="0xabc", chain="base")
    return gate._audit[-1]


def test_an_owner_direct_spend_is_booked_with_its_lane():
    gate = PolicyGate(max_per_tx_usd=5000.0, daily_cap_usd=10000.0)
    d = _authorize(_ctx(), gate, halted=True)
    assert d.allowed and d.lane == "owner_direct"
    assert _record(gate)["lane"] == "owner_direct"


def test_an_autonomous_spend_carries_no_owner_lane():
    gate = PolicyGate(max_per_tx_usd=5000.0, daily_cap_usd=10000.0)
    ctx = _ctx(metadata={tx_guard.OWNER_GRANT_KEY: {"approved": True, "max_spend_usd": 2500.0}})
    d = _authorize(ctx, gate, forged=True)
    assert d.allowed and d.lane == "owner_approved"
    assert _record(gate)["lane"] == "owner_approved"
    # a later record for another key inherits nothing
    assert "lane" not in _record(gate, idem="idem-2")


def test_the_lane_note_is_consumed_once_and_bounded():
    gate = PolicyGate(max_per_tx_usd=5000.0, daily_cap_usd=10000.0)
    gate.note_lane("k", "owner_direct")
    assert _record(gate, idem="k")["lane"] == "owner_direct"
    assert "lane" not in _record(gate, idem="k")
    for i in range(1000):
        gate.note_lane(f"k{i}", "owner_direct")
    assert len(gate._lanes) <= 256


def test_the_spend_telemetry_carries_the_lane(monkeypatch):
    seen = {}

    class _Log:
        def record(self, kind, **kw):
            seen.update(kw)

    monkeypatch.setattr("core.event_log.event_log_enabled", lambda: True)
    monkeypatch.setattr("core.event_log.get_event_log", lambda: _Log())
    from core.wallet.factory import _emit_spend_to_event_log
    _emit_spend_to_event_log({"venue": "defi", "amount_usd": 1.0, "lane": "owner_direct"})
    assert seen.get("lane") == "owner_direct"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "telemetry_events.db"))
    return tmp_path


def _violations(home):
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("rob", data_dir=str(home), include_money=False)
    return [h for h in snap.health if h.key == "pause_violation"]


def test_an_owner_send_during_the_pause_is_not_a_violation(home):
    from core import autonomy_control as ac
    from core.event_log import TelemetryEventLog
    ac.pause(str(home), via="test")
    log = TelemetryEventLog(str(home / "telemetry_events.db"))
    log.record("wallet_spend", user_id="rob", session_id="tg", source="wallet",
               attrs={"amount_usd": 2680.0, "lane": "owner_direct"})
    assert _violations(home) == []


def test_an_agent_spend_during_the_pause_is_still_a_violation(home):
    from core import autonomy_control as ac
    from core.event_log import TelemetryEventLog
    ac.pause(str(home), via="test")
    log = TelemetryEventLog(str(home / "telemetry_events.db"))
    for lane in ("owner_approved", "autonomous", None):
        attrs = {"amount_usd": 5.0}
        if lane:
            attrs["lane"] = lane
        log.record("wallet_spend", user_id="rob", session_id="g", source="wallet",
                   attrs=attrs)
    v = _violations(home)
    assert v and "wallet_spend" in v[0].text
