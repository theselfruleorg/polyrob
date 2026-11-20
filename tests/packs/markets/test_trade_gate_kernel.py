"""125 S7 (A-2; D2): the markets pack's turn gate asks the money kernel.

``trade_turn_refusal`` is two ``authorize_spend`` calls through ``polyrob.sdk``: the
turn origin (a forged turn refused, the autonomous lane never admitted), then the
principal and the pause. A genuine owner turn is the owner acting: it trades during the
autonomy pause and under ``/pause trading`` (D2). A direct call (no context) is never
the owner, so the pause binds it, except a risk-reducing mutation made by hand.
"""
import pytest

pytest.importorskip("polyrob_markets")


def _owner_ctx(**meta):
    from agents.task.loop_types.execution_context import ActionExecutionContext
    ctx = ActionExecutionContext(user_id="local", role="orchestrator")
    if meta:
        ctx.metadata = dict(meta)
    return ctx


@pytest.fixture
def forged(monkeypatch):
    import tools.controller.action_registration as ar
    state = {"forged": False}
    monkeypatch.setattr(ar, "_is_forged_or_autonomous_turn",
                        lambda ctx, tool: state["forged"])
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    return state


@pytest.fixture
def spy(monkeypatch):
    import core.money.authorize as kernel
    calls = []
    real = kernel.authorize_spend

    def _spy(intent, *a, **k):
        calls.append(intent)
        return real(intent, *a, **k)
    monkeypatch.setattr(kernel, "authorize_spend", _spy)
    return calls


def test_the_gate_asks_the_kernel(forged, spy):
    from polyrob_markets.trade_gate import trade_turn_refusal
    assert trade_turn_refusal(_owner_ctx(), object()) is None
    assert spy and spy[0].turn and not spy[0].principal
    assert any(i.principal and i.pause and not i.check_ledger for i in spy)


def test_a_forged_turn_reads_the_forged_sentence(forged, spy):
    from polyrob_markets.trade_gate import trade_turn_refusal
    forged["forged"] = True
    r = trade_turn_refusal(_owner_ctx(), object())
    assert r and "cannot place orders" in r


def test_a_genuine_owner_turn_trades_during_the_halt(forged, monkeypatch):
    from polyrob_markets.trade_gate import trade_turn_refusal
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    assert trade_turn_refusal(_owner_ctx(), object()) is None


def test_a_genuine_owner_turn_trades_under_pause_trading(forged, monkeypatch):
    import core.autonomy_control as ac
    from polyrob_markets.trade_gate import trade_turn_refusal
    real = ac.pause_refusal_text
    monkeypatch.setattr(ac, "pause_refusal_text", lambda kind, **kw: (
        "refused: opening a position is paused (/pause trading)" if kind == "trade_entry"
        else real(kind, **kw)))
    assert trade_turn_refusal(_owner_ctx(), object()) is None
    # ... and a direct call (no context: never the owner) is still held
    r = trade_turn_refusal(None, object())
    assert r and "paused" in r


def test_a_direct_risk_reducing_call_passes_the_halt(forged, monkeypatch):
    from polyrob_markets.trade_gate import trade_turn_refusal
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    assert trade_turn_refusal(None, object(), risk_reducing=True) is None


def test_a_direct_call_without_risk_reducing_is_held_by_the_halt(forged, monkeypatch):
    from polyrob_markets.trade_gate import trade_turn_refusal
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    r = trade_turn_refusal(None, object())
    assert r and "autonomy pause" in r.lower()


def test_an_order_run_is_refused(forged, monkeypatch):
    """An ``ord_`` run is autonomous: the markets gate never admits the autonomous lane."""
    import tools.controller.action_registration as ar
    from polyrob_markets.trade_gate import trade_turn_refusal
    forged["forged"] = True
    monkeypatch.setattr(ar, "_is_autonomous_goal_turn", lambda ctx, tool: True)
    r = trade_turn_refusal(_owner_ctx(mandate="ord_abc"), object())
    assert r and "cannot place orders" in r


def test_evaluate_live_trade_no_longer_probes_the_halt(monkeypatch):
    """The halt is ``trade_turn_refusal``'s bar now (callers run it first)."""
    from polyrob_markets.trade_gate import evaluate_live_trade
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADE_MAX_USD", "50")
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    assert evaluate_live_trade("hyperliquid", 1.0).live is True
