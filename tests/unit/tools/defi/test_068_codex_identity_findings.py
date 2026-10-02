"""Codex review of 068 — findings B1, B2, B3, B4, B10, B11, each reproduced first.

B1  an unreadable pin / position store read as EMPTY lifted the identity gate.
B2  `BASE` reached the chain registry but missed every canonical pin keyed `base`.
B3  Solana mints were compared lowercased: two different accounts were "equal".
B4  target_token bound only swaps on the target chain; USDC->WETH, another chain,
    launchpad buys and child jobs all escaped it.
B10 an owner-pinned Solana mint read `verified: false` in token_info.
B11 reconcile dropped the quote assets before looking for a symbol collision.
"""
import os
from types import SimpleNamespace

import pytest

from core.wallet import token_pins
from core.wallet.buy_target import (acquisition_refusal, inherit_target,
                                    target_from_context)
from tools.defi.identity_gate import buy_identity_refusal

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_WETH = "0x4200000000000000000000000000000000000006"
FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"
REAL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_USDC_CASE = SOL_USDC[:-1] + "V"   # another valid 32-byte key


def _ctx(target=None, uid="rob"):
    meta = {"money_target": target} if target else {}
    return SimpleNamespace(user_id=uid, metadata=meta, role="orchestrator",
                           is_sub_agent=False)


def _ident(symbol, verified=False, source="frozen", name="x"):
    return SimpleNamespace(symbol=symbol, name=name, verified=verified, source=source)


@pytest.fixture
def pins(tmp_path, monkeypatch):
    path = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: path)
    return path


@pytest.fixture
def no_positions(monkeypatch):
    import core.open_positions as op
    monkeypatch.setattr(op, "entries_for", lambda uid, **kw: {})


# ---- B1 ---------------------------------------------------------------------

def _corrupt(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"this is not a sqlite database" * 100)


def test_b1_unreadable_pin_store_refuses_the_codex_scenario(pins, no_positions):
    token_pins.pin("robinhood", REAL, "PNL")
    _corrupt(pins)
    why = buy_identity_refusal(chain="robinhood", token_out=FAKE, id_out=_ident("PNL"),
                               max_spend_usd=100.0, route_verdict="AGREES",
                               execution_context=_ctx())
    assert why and "could not be read" in why


def test_b1_unreadable_store_still_lets_a_canonical_buy_through(pins, no_positions):
    _corrupt(pins)
    assert buy_identity_refusal(
        chain="base", token_out=BASE_USDC,
        id_out=_ident("USDC", verified=True, source="canonical"),
        max_spend_usd=100.0, route_verdict="AGREES", execution_context=_ctx()) is None


def test_b1_absent_store_is_no_pins(pins, no_positions):
    assert token_pins.pins_status()[0] == "absent"
    assert buy_identity_refusal(chain="base", token_out=FAKE, id_out=_ident("MEME"),
                                max_spend_usd=100.0, route_verdict="AGREES",
                                execution_context=_ctx()) is None


def test_b1_pins_status_says_unreadable(pins):
    _corrupt(pins)
    state, rows, err = token_pins.pins_status()
    assert state == "unreadable" and rows == [] and err
    with pytest.raises(token_pins.PinStoreUnreadable):
        token_pins.all_pins(strict=True)


def test_b1_unreadable_position_store_refuses(pins, monkeypatch):
    import core.open_positions as op

    def boom(uid, **kw):
        assert kw.get("strict") is True
        raise RuntimeError("database disk image is malformed")
    monkeypatch.setattr(op, "entries_for", boom)
    why = buy_identity_refusal(chain="robinhood", token_out=FAKE, id_out=_ident("PNL"),
                               max_spend_usd=1.0, route_verdict="AGREES",
                               execution_context=_ctx())
    assert why and "could not be read" in why


def test_b1_entries_for_strict_raises_on_a_corrupt_store(tmp_path):
    from core import open_positions
    db = tmp_path / "open_positions.db"
    db.write_bytes(b"garbage" * 200)
    assert open_positions.entries_for("rob", db_path=str(db)) == {}
    with pytest.raises(Exception):
        open_positions.entries_for("rob", db_path=str(db), strict=True)


# ---- B2 ---------------------------------------------------------------------

@pytest.mark.parametrize("chain", ["base", "BASE", "  Base "])
def test_b2_fake_usdc_is_refused_whatever_the_chain_case(pins, no_positions, chain):
    why = buy_identity_refusal(chain=chain, token_out=FAKE, id_out=_ident("usdc"),
                               max_spend_usd=1.0, route_verdict="AGREES",
                               execution_context=_ctx())
    assert why and "PINNED" in why


def test_b2_owner_pin_and_tracked_positions_fold_the_chain(pins, monkeypatch):
    token_pins.pin(" RobinHood ", REAL, "PNL")
    assert token_pins.pinned_addresses_for_symbol("ROBINHOOD", "pnl") == [REAL]
    import core.open_positions as op
    held = SimpleNamespace(chain="robinhood", address=REAL, symbol="PNL")
    monkeypatch.setattr(op, "entries_for", lambda uid, **kw: {REAL.lower(): held})
    token_pins.unpin("robinhood", REAL)
    why = buy_identity_refusal(chain="ROBINHOOD", token_out=FAKE, id_out=_ident("PNL"),
                               max_spend_usd=1.0, route_verdict="AGREES",
                               execution_context=_ctx())
    assert why and "two contracts" in why


def test_b2_every_money_verb_param_folds_the_chain():
    from tools.defi import trade_tool as t
    p = t.SwapParams(chain=" BASE ", token_in="native", token_out=BASE_USDC,
                     amount_in=1, max_spend_usd=1)
    a = t.ApproveParams(chain="Base", token=BASE_USDC, spender=BASE_WETH,
                        amount=1, max_spend_usd=1)
    c = t.CallParams(chain="ROBINHOOD", to=BASE_WETH, calldata="0x", max_spend_usd=1)
    assert (p.chain, a.chain, c.chain) == ("base", "base", "robinhood")


def test_b2_canonical_lookup_folds_the_chain():
    from core.wallet.tokens import canonical_token
    assert canonical_token("BASE", BASE_USDC) is not None


# ---- B3 ---------------------------------------------------------------------

def test_b3_solana_pin_does_not_match_a_case_variant(pins):
    token_pins.pin("solana", SOL_USDC, "USDC")
    assert token_pins.owner_pin("solana", SOL_USDC) is not None
    assert token_pins.owner_pin("solana", SOL_USDC_CASE) is None
    assert token_pins.unpin("solana", SOL_USDC_CASE) is False
    assert token_pins.unpin("solana", SOL_USDC) is True


def test_b3_evm_pin_still_matches_any_case(pins):
    token_pins.pin("robinhood", REAL, "PNL")
    assert token_pins.owner_pin("robinhood", REAL.lower()) is not None


def test_b3_collision_lines_keep_base58_case():
    from tools.defi.reconcile import _ticker_collision_lines
    out = "\n".join(_ticker_collision_lines([("X", SOL_USDC), ("X", SOL_USDC_CASE)]))
    assert "2 contracts" in out


# ---- B4 ---------------------------------------------------------------------

T = {"chain": "robinhood", "address": REAL}


def test_b4_other_chain_is_refused_under_a_target():
    assert acquisition_refusal(_ctx(T), chain="base", token_out=FAKE)


def test_r3_canonical_conversions_are_working_capital_not_acquisitions():
    """Decided after the third review: under a target, moving working capital
    between canonical assets (USDC -> WETH, native -> USDC) is allowed; only a
    NON-canonical token other than the target is an acquisition."""
    t = {"chain": "base", "address": FAKE}
    assert acquisition_refusal(_ctx(t), chain="base", token_out=BASE_WETH,
                               token_in=BASE_USDC) is None
    assert acquisition_refusal(_ctx(t), chain="base", token_out=BASE_USDC,
                               native_in=True) is None
    assert acquisition_refusal(_ctx(t), chain="base", token_out=REAL, token_in=BASE_USDC)


def test_b4_selling_a_held_token_into_the_quote_asset_is_the_exit():
    t = {"chain": "base", "address": FAKE}
    assert acquisition_refusal(_ctx(t), chain="base", token_out=BASE_USDC,
                               token_in=REAL) is None


def test_b4_the_target_itself_and_a_run_without_one_pass():
    assert acquisition_refusal(_ctx(T), chain="ROBINHOOD", token_out=REAL.lower()) is None
    assert acquisition_refusal(_ctx(None), chain="base", token_out=FAKE) is None


def test_b4_positions_and_deployments_are_refused_under_a_target():
    assert acquisition_refusal(_ctx(T), chain="robinhood", token_out=None, what="launch")


def test_b4_child_work_inherits_and_may_only_restate_the_target():
    assert inherit_target(_ctx(T), None) == T
    assert inherit_target(_ctx(T), {"chain": "robinhood", "address": REAL.lower()})
    with pytest.raises(ValueError):
        inherit_target(_ctx(T), {"chain": "robinhood", "address": FAKE})
    with pytest.raises(ValueError):
        inherit_target(_ctx(T), {"chain": "base", "address": REAL})
    assert inherit_target(_ctx(None), None) is None


def test_b4_every_acquiring_verb_consults_the_boundary():
    import inspect
    from tools.defi.trade_tool import DefiTradeTool
    from tools.goal_tools import GoalTool as GoalTools
    from tools.launchpad.tool import LaunchpadTool
    import tools.cronjob_tools as cj
    for fn in (DefiTradeTool.swap, DefiTradeTool.solana_swap, DefiTradeTool.call,
               DefiTradeTool.lp_add, DefiTradeTool.deploy_token,
               DefiTradeTool.deploy_contract, DefiTradeTool.solana_deploy_token,
               LaunchpadTool.launchpad_buy, LaunchpadTool.launchpad_launch):
        assert "acquisition_refusal" in inspect.getsource(fn), fn.__name__
    assert "buy_identity_refusal" in inspect.getsource(LaunchpadTool.launchpad_buy)
    assert "inherit_target" in inspect.getsource(GoalTools.goal_create)
    assert inspect.getsource(cj).count("inherit_target(") >= 2


def test_b4_solana_target_check_covers_canonical_outputs():
    import inspect
    from tools.defi.trade_tool import DefiTradeTool
    src = inspect.getsource(DefiTradeTool.solana_swap)
    assert src.index("acquisition_refusal") < src.index("if token_out not in (usdc_mint")


# ---- B10 --------------------------------------------------------------------

def test_b10_owner_pinned_solana_mint_reads_verified(pins):
    from tools.defi.data_tool import DefiDataTool
    mint = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    token_pins.pin("solana", mint, "ROB")
    ident = DefiDataTool._identity(SimpleNamespace(_identity_fn=None), "solana", mint)
    assert ident.verified and ident.source == "owner_pin"
    assert ident.decimals is None and ident.name is None     # nothing invented
    other = DefiDataTool._identity(SimpleNamespace(_identity_fn=None), "solana",
                                   mint[:-1] + mint[-1].swapcase())
    assert not other.verified


# ---- B11 --------------------------------------------------------------------

def _h(addr, sym, name):
    from tools.defi.reconcile import ChainHolding
    return ChainHolding(address=addr, symbol=sym, qty=1.0, raw_units=1,
                        value_usd=None, balance_known=True, name=name)


def test_b11_real_and_fake_usdc_warn():
    from tools.defi import reconcile as rec
    report = rec.diff([], [_h(BASE_USDC, "USDC", "USD Coin"), _h(FAKE, "USDC", "Fake")],
                      quote_addresses=[BASE_USDC])
    assert report.collisions and "Fake" in "\n".join(report.collisions)
    assert not any(BASE_USDC in u for u in report.unexplained)   # still working capital


def test_b11_fake_usdc_warns_even_with_no_real_usdc_held():
    from tools.defi import reconcile as rec
    report = rec.diff([], [_h(FAKE, "USDC", "Fake")], quote_addresses=[BASE_USDC])
    assert report.collisions
