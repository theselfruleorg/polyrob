"""tx_guard's turn-origin bar, narrowed for the treasury rail (owner decision).

Step 2 of `authorize` refuses ANY forged-or-autonomous turn. That correctly kills
self-wake, delegation-result and leaf/sub-agent trading -- but a goal-dispatched
run is also "autonomous", so it refused the very loop the owner asked for.

`DEFI_AUTONOMOUS_TURN_TRADING` (default OFF) narrows the bar to let a
goal/cron-dispatched MAIN-agent session through, and nothing else. Every other
origin still refuses, and the caps/simulation/taint bars are untouched.
"""
import pytest

from core.wallet import tx_guard


class _Gate:
    def reserve(self):
        raise AssertionError("authorize must refuse before reserving")

    def evaluate(self, **kw):
        raise AssertionError("must not reach PolicyGate")


def _intent():
    return tx_guard.TxIntent(chain="base", token="0x" + "1" * 40, to="0x" + "2" * 40,
                             amount_raw=1, max_spend_usd=0.5, idempotency_key="k")


def _authorize(*, forged, autonomous_ok, halted=False):
    """Run authorize far enough to observe the turn-origin decision only."""
    return tx_guard.authorize(
        _intent(), {"to": "0x" + "2" * 40, "data": "0x"},
        holder="0x" + "3" * 40, gate=_Gate(),
        execution_context=__import__("types").SimpleNamespace(user_id="local"), tool_self=None,
        halted_fn=lambda: halted,
        forged_fn=lambda _ec, _ts: forged,
        autonomous_ok_fn=(None if autonomous_ok is None
                          else (lambda _ec, _ts: autonomous_ok)),
        rpc_is_pinned_fn=lambda _c: False,  # refuses right after the origin bar
    )


class TestFlagOff:
    @pytest.fixture(autouse=True)
    def _off(self, monkeypatch):
        monkeypatch.delenv("DEFI_AUTONOMOUS_TURN_TRADING", raising=False)

    def test_an_autonomous_turn_is_refused(self):
        d = _authorize(forged=True, autonomous_ok=True)
        assert not d.allowed and "forged/autonomous turn" in d.reason

    def test_a_genuine_turn_passes_the_origin_bar(self):
        d = _authorize(forged=False, autonomous_ok=False)
        assert "forged/autonomous turn" not in d.reason


class TestFlagOn:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv("DEFI_AUTONOMOUS_TURN_TRADING", "true")

    def test_a_goal_dispatched_turn_passes_the_origin_bar(self):
        d = _authorize(forged=True, autonomous_ok=True)
        assert "forged/autonomous turn" not in d.reason, d.reason

    def test_a_leaf_or_self_wake_turn_is_still_refused(self):
        """autonomous_ok_fn answers False for leaf / self-wake / delegation-result."""
        d = _authorize(forged=True, autonomous_ok=False)
        assert not d.allowed and "forged/autonomous turn" in d.reason

    def test_without_the_detector_it_still_refuses(self):
        """No prober => cannot prove WHICH kind of autonomous turn this is."""
        d = _authorize(forged=True, autonomous_ok=None)
        assert not d.allowed and "forged/autonomous turn" in d.reason

    def test_a_raising_detector_fails_closed(self):
        def _boom(_ec, _ts):
            raise RuntimeError("probe is broken")
        d = tx_guard.authorize(
            _intent(), {"to": "0x" + "2" * 40, "data": "0x"},
            holder="0x" + "3" * 40, gate=_Gate(),
            execution_context=__import__("types").SimpleNamespace(user_id="local"), tool_self=None,
            halted_fn=lambda: False,
            forged_fn=lambda _ec, _ts: True,
            autonomous_ok_fn=_boom,
            rpc_is_pinned_fn=lambda _c: False)
        assert not d.allowed and "forged/autonomous turn" in d.reason

    def test_the_owner_pause_still_wins(self):
        """The owner's stop beats the autonomous lane, whatever the flag says.

        Renamed from `test_the_kill_switch_still_wins` and re-pointed at the CLAIM
        rather than the word: it asserted the literal "HALTED" from the legacy
        text, which named a lever that does not exist (`autonomy_halted()` is
        `not allows("dispatch").allowed`, a facet of the 031 pause record). Pinning
        that wording is what let the phantom survive in `tx_guard` for a day after
        `125f83dd` removed it from the bridge. What must hold is that it REFUSES
        and says why (census, 2026-09-12).
        """
        d = _authorize(forged=True, autonomous_ok=True, halted=True)
        assert not d.allowed
        assert "pause" in d.reason.lower()
        assert "(owner kill-switch)" not in d.reason


class TestAutonomousLaneNeedsADailyCap:
    """An unattended (goal-dispatched) spend must have an aggregate damage bound.

    The per-tx ceiling alone cannot stop an injection during a trade leg from
    looping within-ceiling spends that drain the treasury one ticket at a time,
    so tx_guard refuses the autonomous lane when no WALLET_DAILY_CAP_USD is set.
    An owner-driven turn is unaffected.
    """

    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv("DEFI_AUTONOMOUS_TURN_TRADING", "true")
        # a $1 send: outflow 1_000_000 units at 6 decimals * $1.00
        monkeypatch.setattr(tx_guard, "_decimals_for", lambda _c, _t: 6)

    def _run(self, gate, *, forged, autonomous_ok):
        from core.wallet.simulation import Deltas
        token = "0x" + "1" * 40
        intent = tx_guard.TxIntent(chain="base", token=token, to="0x" + "2" * 40,
                                   amount_raw=1_000_000, max_spend_usd=5.0,
                                   idempotency_key="k")
        return tx_guard.authorize(
            intent, {"to": "0x" + "2" * 40, "data": "0x"},
            holder="0x" + "3" * 40, gate=gate,
            execution_context=__import__("types").SimpleNamespace(user_id="local"), tool_self=None,
            halted_fn=lambda: False,
            forged_fn=lambda _ec, _ts: forged,
            autonomous_ok_fn=lambda _ec, _ts: autonomous_ok,
            rpc_is_pinned_fn=lambda _c: True,
            simulate_fn=lambda **kw: Deltas(ok=True, token_deltas={token: -1_000_000},
                                            gas_used=100_000),
            price_fn=lambda _c, _t: 1.0)

    def test_autonomous_turn_with_no_daily_cap_refuses(self):
        from core.wallet.policy import PolicyGate
        gate = PolicyGate(max_per_tx_usd=100.0)  # no daily_cap_usd
        d = self._run(gate, forged=True, autonomous_ok=True)
        assert not d.allowed
        assert "WALLET_DAILY_CAP_USD" in d.reason
        assert d.lane == "owner_queue"

    def test_autonomous_turn_with_a_daily_cap_passes(self):
        from core.wallet.policy import PolicyGate
        gate = PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=10.0)
        d = self._run(gate, forged=True, autonomous_ok=True)
        assert d.allowed and d.lane == "autonomous"

    def test_owner_turn_with_no_daily_cap_is_unaffected(self):
        from core.wallet.policy import PolicyGate
        gate = PolicyGate(max_per_tx_usd=100.0)  # no daily_cap_usd
        d = self._run(gate, forged=False, autonomous_ok=False)
        assert d.allowed and d.lane == "autonomous"


@pytest.fixture(autouse=True)
def _wallet_owner_identity(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "local")
