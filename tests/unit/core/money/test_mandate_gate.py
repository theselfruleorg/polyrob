"""086 W2: the trading mandate's step in the money kernel + the breakers."""
import types

import pytest

import core.money.authority as authority
from core import trading_mandate as tm
from core.money import mandate_gate as mg
from core.money.authorize import SpendIntent, authorize_spend
from core.money.mandate_gate import ClosedTrade, entry_refusal, evaluate_breakers
from core.open_positions import PositionDelta, apply_delta

MEME = "0xb200000000000000000000ea8625786a776539fb"
MEME2 = "0xb200000000000000000000b344cb4a1e8bd51968"
T0 = 1_000_000.0


@pytest.fixture
def paths(tmp_path):
    return str(tmp_path / "trading.db"), str(tmp_path / "open_positions.db")


@pytest.fixture
def mandate(paths):
    mpath, _ = paths
    m = tm.create("u1", actor=tm.OWNER_SEAT, budget_usd=10.0, chains=["base", "solana"],
                  slots=2, path=mpath, now=T0)
    # Full tickets: the stage cap has its own test below.
    return tm.set_stage(m.id, "normal", path=mpath, now=T0)


def test_an_entry_costs_at_most_the_stage_ticket(paths):
    mpath, _ = paths
    m = tm.create("u1", actor=tm.OWNER_SEAT, budget_usd=10.0, chains=["base"], path=mpath,
                  now=T0)                                    # probe: $1 tickets
    assert "probe stage" in _ref(m, paths, usd=1.2)
    assert _ref(m, paths, usd=1.05) is None                  # within the tolerance
    assert _ref(m, paths, usd=None) is None
    m = tm.set_stage(m.id, "small", path=mpath, now=T0)      # half of $3
    assert _ref(m, paths, usd=1.5) is None and "small stage" in _ref(m, paths, usd=2.0)


def _buy(book, token, cost, ts, chain="base", user="u1"):
    apply_delta(user, PositionDelta(chain=chain, address=token, symbol="X", qty=100.0,
                                    cost_usd=cost), db_path=book, now=ts)


def _sell_all(book, token, proceeds, ts, chain="base"):
    apply_delta("u1", PositionDelta(chain=chain, address=token, symbol="X", qty=-100.0,
                                    proceeds_usd=proceeds), db_path=book, now=ts)


def _ref(mandate, paths, token=MEME, usd=3.0, chain="base", now=T0 + 100, user="u1"):
    mpath, book = paths
    return entry_refusal(mandate.id, user_id=user, chain=chain, token=token, usd=usd,
                         now=now, path=mpath, book_path=book)


def test_an_active_mandate_with_an_empty_book_admits(mandate, paths):
    assert _ref(mandate, paths) is None


@pytest.mark.parametrize("status,word", [("paused", "PAUSED"), ("stopped", "STOPPED")])
def test_a_paused_or_stopped_mandate_refuses(mandate, paths, status, word):
    tm.set_status(mandate.id, status, actor=tm.OWNER_SEAT, path=paths[0])
    assert word in _ref(mandate, paths)


def test_a_halted_mandate_refuses_and_names_the_breaker(mandate, paths):
    tm.latch(mandate.id, "breaker:daily_loss", path=paths[0])
    why = _ref(mandate, paths)
    assert "HALTED" in why and "daily_loss" in why and "/trade release" in why


def test_a_cooldown_refuses_then_expires(mandate, paths):
    tm.set_cooldown(mandate.id, 6, path=paths[0], now=T0)
    assert "cooling down" in _ref(mandate, paths, now=T0 + 3600)
    assert _ref(mandate, paths, now=T0 + 6 * 3600 + 1) is None


def test_another_chain_refuses(mandate, paths):
    assert "trades on base, solana only" in _ref(mandate, paths, chain="robinhood")


def test_another_user_refuses(mandate, paths):
    assert "another user" in _ref(mandate, paths, user="u2")


def test_unknown_mandate_fails_closed(paths, mandate):
    mpath, book = paths
    why = entry_refusal("m_nope", user_id="u1", chain="base", token=MEME, usd=1.0,
                        path=mpath, book_path=book)
    assert "does not exist" in why


def test_unreadable_mandate_fails_closed(tmp_path, paths):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"garbage" * 200)
    why = entry_refusal("m", user_id="u1", chain="base", token=MEME, usd=1.0,
                        path=str(bad), book_path=paths[1])
    assert "could not be read" in why and "failing closed" in why


def test_unreadable_book_fails_closed(mandate, paths, tmp_path):
    bad = tmp_path / "book.db"
    bad.write_bytes(b"garbage" * 200)
    why = entry_refusal(mandate.id, user_id="u1", chain="base", token=MEME, usd=1.0,
                        now=T0 + 1, path=paths[0], book_path=str(bad))
    assert "position book could not be read" in why


def test_slots_full_refuses_but_adding_to_a_held_token_does_not(mandate, paths):
    _buy(paths[1], MEME, 3.0, T0 + 10)
    _buy(paths[1], MEME2, 3.0, T0 + 20)
    other = "0xb200000000000000000000000000000000000001"
    assert "slots in use" in _ref(mandate, paths, token=other, usd=1.0)
    assert _ref(mandate, paths, token=MEME, usd=1.0) is None


def test_budget_at_cost(mandate, paths):
    _buy(paths[1], MEME, 8.0, T0 + 10)
    assert "allocation is used" in _ref(mandate, paths, token=MEME, usd=3.0)
    assert _ref(mandate, paths, token=MEME, usd=2.0) is None


def test_positions_before_the_mandate_are_not_its_own(mandate, paths):
    _buy(paths[1], MEME, 50.0, T0 - 10)  # e.g. a rebuy row held before the mandate
    _buy(paths[1], MEME2, 50.0, T0 - 5)
    assert _ref(mandate, paths, token="0xb200000000000000000000000000000000000002") is None


def test_unknown_basis_counts_as_a_full_ticket(mandate, paths):
    apply_delta("u1", PositionDelta(chain="base", address=MEME, symbol="X", qty=1.0,
                                    cost_usd=None), db_path=paths[1], now=T0 + 10)
    # 10 budget - 3 ticket = 7 left (the stage cap is lifted to isolate the allocation)
    import core.money.mandate_gate as mg
    mg_cap = mg._stage_refusal
    try:
        mg._stage_refusal = lambda m, usd: None
        assert _ref(mandate, paths, token=MEME, usd=7.0) is None
        assert "allocation is used" in _ref(mandate, paths, token=MEME, usd=7.5)
    finally:
        mg._stage_refusal = mg_cap


def test_daily_entries(paths):
    mpath, _ = paths
    m = tm.create("u1", actor=tm.OWNER_SEAT, budget_usd=100.0, chains=["base"], path=mpath,
                  now=T0, breakers={"max_entries_per_day": 2})
    m = tm.set_stage(m.id, "normal", path=mpath, now=T0)
    tm.record_entry(m.id, chain="base", token="a", path=mpath, now=T0 + 10)
    assert _ref(m, paths) is None
    tm.record_entry(m.id, chain="base", token="b", path=mpath, now=T0 + 20)
    assert "2 entries in 24 h" in _ref(m, paths)
    assert _ref(m, paths, now=T0 + 10 + 86400 + 30) is None


def test_token_cooldown(mandate, paths):
    _buy(paths[1], MEME, 3.0, T0 + 10)
    _sell_all(paths[1], MEME, 1.0, T0 + 20)
    assert "token cooldown" in _ref(mandate, paths, now=T0 + 3600)
    assert _ref(mandate, paths, now=T0 + 20 + 24 * 3600 + 1) is None
    assert _ref(mandate, paths, token=MEME2, now=T0 + 3600) is None


# -- authorize_spend wiring ---------------------------------------------------------

@pytest.fixture
def kernel(monkeypatch, paths, mandate):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    monkeypatch.setattr(authority, "spend_pause_refusal", lambda entry=False: None)
    mpath, book = paths
    real = mg.entry_refusal
    monkeypatch.setattr(mg, "entry_refusal",
                        lambda *a, **k: real(*a, **{**k, "path": mpath, "book_path": book}))
    tm.latch(mandate.id, "breaker:drawdown", path=mpath)
    return mandate


def _ctx(mandate_id):
    return types.SimpleNamespace(user_id="u1", role="orchestrator", is_sub_agent=False,
                                 metadata={"mandate": mandate_id} if mandate_id else {})


def _forged(ctx, tool):
    return True


def _intent(**kw):
    d = dict(mandate=True, entry=True, chain="base", token=MEME, usd=1.0)
    d.update(kw)
    return SpendIntent.turn_origin(**d)


def test_kernel_refuses_a_mandate_entry(kernel):
    v = authorize_spend(_intent(), _ctx(kernel.id), forged_fn=_forged,
                        autonomous_ok_fn=lambda c, t: True)
    assert v.refused and v.step == "mandate" and "HALTED" in v.reason


def test_kernel_skips_exits_dry_runs_and_runs_with_no_mandate(kernel):
    ok = dict(forged_fn=_forged, autonomous_ok_fn=lambda c, t: True)
    assert authorize_spend(_intent(entry=False), _ctx(kernel.id), **ok).allowed
    assert authorize_spend(_intent(dry_run=True), _ctx(kernel.id), **ok).allowed
    assert authorize_spend(_intent(mandate=False), _ctx(kernel.id), **ok).allowed
    assert authorize_spend(_intent(), _ctx(None), **ok).allowed
    assert authorize_spend(_intent(), None, **ok).allowed


def test_kernel_never_gates_a_genuine_owner_turn(kernel):
    v = authorize_spend(_intent(), _ctx(kernel.id), forged_fn=lambda c, t: False)
    assert v.allowed


def test_kernel_fails_closed_when_the_probe_raises(kernel, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(mg, "entry_refusal", boom)
    v = authorize_spend(_intent(), _ctx(kernel.id), forged_fn=_forged,
                        autonomous_ok_fn=lambda c, t: True)
    assert v.refused and v.step == "mandate" and "failing closed" in v.reason


# -- breakers (pure) ----------------------------------------------------------------

def _m(**kw):
    d = dict(id="m", user_id="u", status="active", budget_usd=100.0, chains=("base",),
             preset="memecoin", ticket_usd=3.0, slots=8, actor=tm.OWNER_SEAT,
             created_at=0.0, updated_at=0.0)
    d.update(kw)
    return tm.Mandate(**d)


NOW = 10 * 86400.0


def test_no_trades_no_breaker():
    assert evaluate_breakers(_m(), [], NOW) is None


def test_daily_loss():
    rows = [ClosedTrade(-6.0, NOW - 3600), ClosedTrade(-4.0, NOW - 60)]
    assert evaluate_breakers(_m(), rows, NOW) == "daily_loss"
    old = [ClosedTrade(-6.0, NOW - 2 * 86400), ClosedTrade(-4.0, NOW - 60)]
    assert evaluate_breakers(_m(), old, NOW) is None


def test_drawdown_from_peak():
    rows = [ClosedTrade(+20.0, NOW - 5 * 86400), ClosedTrade(-15.0, NOW - 4 * 86400),
            ClosedTrade(-15.0, NOW - 3 * 86400)]
    assert evaluate_breakers(_m(), rows, NOW) == "drawdown"


def test_unknown_pnl_is_skipped_never_zero():
    rows = [ClosedTrade(None, NOW - 60)] * 10
    assert evaluate_breakers(_m(), rows, NOW) is None


def test_consecutive_losses_then_cooldown_resets_the_count():
    rows = [ClosedTrade(-0.5, NOW - 86400 * 2 - i) for i in range(4)]
    assert evaluate_breakers(_m(), rows, NOW) == "consecutive_losses"
    assert evaluate_breakers(_m(cooldown_at=NOW - 3600, cooldown_until=NOW + 3600),
                             rows, NOW) is None
    # after the cooldown: the losses that tripped it no longer count
    assert evaluate_breakers(_m(cooldown_at=NOW - 86400, cooldown_until=NOW - 3600),
                             rows, NOW) is None
    rows2 = [ClosedTrade(+1.0, NOW - 86400 * 2 - 10)] + rows
    assert evaluate_breakers(_m(), rows2, NOW) == "consecutive_losses"
    rows3 = rows + [ClosedTrade(+1.0, NOW - 60)]
    assert evaluate_breakers(_m(), rows3, NOW) is None


def test_cost_over_allocation():
    assert evaluate_breakers(_m(), [], NOW, llm_cost_usd=10.0) == "cost_over_alloc"
    assert evaluate_breakers(_m(), [], NOW, llm_cost_usd=9.0) is None


def test_trades_before_the_mandate_do_not_count():
    rows = [ClosedTrade(-50.0, 10.0)]
    assert evaluate_breakers(_m(created_at=100.0), rows, NOW) is None


def test_a_halted_mandate_trips_nothing():
    rows = [ClosedTrade(-50.0, NOW - 60)]
    assert evaluate_breakers(_m(status="halted"), rows, NOW) is None


def test_owner_tuned_breakers_apply():
    rows = [ClosedTrade(-6.0, NOW - 60)]
    assert evaluate_breakers(_m(), rows, NOW) is None
    assert evaluate_breakers(_m(breakers={"daily_loss_pct": 5.0}), rows, NOW) == "daily_loss"


# -- the callers: tx_guard step 2 (EVM) and the SVM turn gate ------------------------

def _guard_ctx(mandate_id, inflow=MEME, chain="base"):
    intent = types.SimpleNamespace(inflow_token=inflow, chain=chain, max_spend_usd=1.0,
                                   token="0xusdc")
    return types.SimpleNamespace(intent=intent, execution_context=_ctx(mandate_id),
                                 tool_self=None, forged_fn=_forged,
                                 autonomous_ok_fn=lambda c, t: True)


@pytest.fixture
def guard(monkeypatch, kernel):
    from core.wallet import tx_guard as _tg
    monkeypatch.setattr(_tg, "_autonomous_turn_allowed", lambda ctx, ts, fn: True)
    monkeypatch.setattr(_tg, "_principal_refusal", lambda ctx: None)
    monkeypatch.setattr(_tg, "_record_money_refusal", lambda *a, **k: None)
    return _tg


def test_tx_guard_refuses_a_mandate_entry(guard, kernel, monkeypatch):
    from core.wallet.tx_guard_steps.turn import step_2_turn_origin
    monkeypatch.setattr(guard, "_intent_is_exit_shaped", lambda i: False)
    d = step_2_turn_origin(_guard_ctx(kernel.id))
    assert d is not None and not d.allowed and "HALTED" in d.reason


def test_tx_guard_lets_an_exit_or_a_send_through(guard, kernel, monkeypatch):
    from core.wallet.tx_guard_steps.turn import step_2_turn_origin
    monkeypatch.setattr(guard, "_intent_is_exit_shaped", lambda i: True)
    assert step_2_turn_origin(_guard_ctx(kernel.id)) is None
    monkeypatch.setattr(guard, "_intent_is_exit_shaped", lambda i: False)
    assert step_2_turn_origin(_guard_ctx(kernel.id, inflow=None)) is None  # a send
    assert step_2_turn_origin(_guard_ctx(None)) is None  # no mandate on the run


def test_svm_turn_gate_refuses_a_mandate_entry(kernel, monkeypatch, paths):
    from core.wallet import tx_guard as _tg
    from tools.defi.trade_tool import DefiTradeTool
    import tools.controller.action_registration as ar
    import tools.controller.turn_origin as to
    monkeypatch.setattr(_tg, "_halted", lambda: False)
    monkeypatch.setattr(_tg, "_entry_paused", lambda: False)
    monkeypatch.setattr(_tg, "_autonomous_turn_allowed", lambda ctx, ts, fn: True)
    monkeypatch.setattr(ar, "_is_forged_or_autonomous_turn", lambda c, t: True)
    monkeypatch.setattr(to, "_is_forged_or_autonomous_turn", lambda c, t: True)
    import core.wallet.authority as wa
    monkeypatch.setattr(wa, "turn_refusal", lambda ctx: None)
    tool = DefiTradeTool.__new__(DefiTradeTool)
    m = tm.create("u9", actor=tm.OWNER_SEAT, budget_usd=10, chains=["solana"], path=paths[0])
    tm.latch(m.id, "breaker:daily_loss", path=paths[0])
    ctx = types.SimpleNamespace(user_id="u9", role="orchestrator", is_sub_agent=False,
                                metadata={"mandate": m.id})
    why, auto, _ = tool._solana_turn_gate(ctx, exit_shaped_fn=lambda: False,
                                          mandate_entry={"token": "Mint1", "usd": 1.0})
    assert why and "HALTED" in why
    assert tool._solana_turn_gate(ctx, exit_shaped_fn=lambda: True,
                                  mandate_entry={"token": "Mint1", "usd": 1.0})[0] is None
    assert tool._solana_turn_gate(ctx, exit_shaped_fn=lambda: False)[0] is None


def test_a_release_is_the_breakers_new_baseline():
    rows = [ClosedTrade(+20.0, NOW - 5 * 86400), ClosedTrade(-30.0, NOW - 3600)]
    assert evaluate_breakers(_m(), rows, NOW) == "drawdown"
    # the owner released after those closes: the same history cannot re-trip
    released = _m(released_at=NOW - 60)
    assert evaluate_breakers(released, rows, NOW) is None
    assert evaluate_breakers(released, rows + [ClosedTrade(-30.0, NOW - 30)], NOW) == "drawdown"
