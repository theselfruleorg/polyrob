"""DEFI-5: rule 1 of the buy identity gate covers OUR OWN launches' symbols.

Rule 1 refused a different contract only for canonical and pinned symbols. The
instance's own launch (PNL) had no rule-1 protection, so a trusted-by-target
look-alike "PNL" passed and quarantined the real position. A contract that
takes the symbol of a token this instance launched is now refused, whatever
else vouches for it — unless it is itself ours or the owner pinned it.
"""
import types

import pytest

from core import open_positions as op
from core.wallet import token_pins
from core.wallet import token_provenance as tp
from core.wallet import tokens
from tools.defi.identity_gate import buy_identity_refusal

REAL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"


def _ctx(target=None):
    meta = {"money_target": target} if target else {}
    return types.SimpleNamespace(user_id="rob", metadata=meta, role="orchestrator",
                                 is_sub_agent=False)


def _ident(symbol="PNL", verified=False, source="frozen"):
    return types.SimpleNamespace(symbol=symbol, name="x", verified=verified, source=source)


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    tp._reset_for_tests()
    monkeypatch.setattr(tp, "_PROBES", {})
    pins = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: pins)
    monkeypatch.setattr(tp, "provenance_db_path", lambda data_home=None: str(tmp_path / "prov.db"))
    monkeypatch.setattr(op, "open_positions_db_path",
                        lambda data_dir=None: str(tmp_path / "open_positions.db"))
    symbols = {REAL.lower(): "PNL"}
    monkeypatch.setattr(tokens, "frozen_record",
                        lambda chain, addr, **k: ({"symbol": symbols[addr.lower()]}
                                                  if addr.lower() in symbols else None))
    tp.record_own_token("robinhood", REAL, kind="launchpad_launch", evidence="tx")
    yield
    tp._reset_for_tests()


def _buy(token, ctx=None, ident=None):
    return buy_identity_refusal(chain="robinhood", token_out=token, id_out=ident or _ident(),
                                max_spend_usd=50.0, route_verdict="AGREES",
                                execution_context=ctx or _ctx())


def test_lookalike_of_our_own_launch_is_refused_even_as_owner_target():
    target = {"chain": "robinhood", "address": FAKE, "authored_by": "owner"}
    why = _buy(FAKE, ctx=_ctx(target))
    assert why and "OUR OWN launch" in why and REAL in why


def test_our_own_launch_itself_passes():
    assert _buy(REAL) is None


def test_a_different_symbol_is_not_touched():
    assert _buy(FAKE, ident=_ident(symbol="OTHER")) is None


def test_an_owner_pin_of_the_other_contract_stands():
    token_pins.pin("robinhood", FAKE, "PNL")
    assert _buy(FAKE, ident=_ident(verified=True, source="owner_pin")) is None
