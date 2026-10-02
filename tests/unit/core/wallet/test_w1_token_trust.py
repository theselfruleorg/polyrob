"""W1: the owner's token-trust decisions, from a chat seat — never from the agent.

* ``trust`` writes an ``owner_approved`` binding keyed on (chain, address) into
  the ONE pin store; ``get_token_identity`` reads it verified with that source.
* ``untrust`` records a NOT-trusted verdict and quarantines a held position; the
  identity gate then refuses the buy outright.
* ``write_off`` needs a confirm and books realized loss = cost basis.
* every writer refuses anything but a genuine owner turn.
"""
import sqlite3
import types

import pytest

from core import open_positions as op
from core.wallet import token_pins
from core.wallet import token_provenance as tp
from core.wallet import token_trust as tt

REAL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    tp._reset_for_tests()
    monkeypatch.setattr(tp, "_PROBES", {})
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")
    pins = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: pins)
    monkeypatch.setattr(tp, "provenance_db_path",
                        lambda data_home=None: str(tmp_path / "wallet" / "prov.db"))
    book = str(tmp_path / "open_positions.db")
    monkeypatch.setattr(op, "open_positions_db_path", lambda data_dir=None: book)
    op.apply_delta("rob", op.PositionDelta(chain="robinhood", address=FAKE.lower(),
                                           symbol="PNL", qty=33_700_000.0,
                                           cost_usd=134.54))
    yield pins
    tp._reset_for_tests()


def _owner():
    return tt.owner_seat_ctx("rob")


def test_trust_writes_an_owner_approved_binding():
    ok, msg = tt.trust(_owner(), "robinhood", REAL, "PNL")
    assert ok, msg
    row = token_pins.owner_pin("robinhood", REAL)
    assert row["source"] == token_pins.SOURCE_OWNER_APPROVED
    assert "owner approved" in msg


def test_identity_reads_the_approved_source(monkeypatch, tmp_path):
    tt.trust(_owner(), "robinhood", REAL, "PNL")
    from core.wallet import tokens
    ident = tokens.TokenIdentity(chain="robinhood", address=REAL, symbol="PNL",
                                 name="Rob Track Record", decimals=18, verified=False,
                                 metadata_changed=False, source="frozen")
    monkeypatch.setattr(tokens, "_identity_unpinned", lambda *a, **k: ident)
    got = tokens.get_token_identity("robinhood", REAL)
    assert got.verified and got.source == "owner_approved"


@pytest.mark.parametrize("ctx", [
    None,
    types.SimpleNamespace(user_id="rob", role="orchestrator", is_sub_agent=True, metadata={}),
    types.SimpleNamespace(user_id="rob", role="leaf", is_sub_agent=False, metadata={}),
    types.SimpleNamespace(user_id="rob", role="orchestrator", is_sub_agent=False,
                          metadata={"turn_kind": "self_wake"}),
])
def test_no_writer_accepts_anything_but_an_owner_turn(ctx):
    for fn in (lambda: tt.trust(ctx, "robinhood", REAL, "PNL"),
               lambda: tt.untrust(ctx, "robinhood", FAKE),
               lambda: tt.write_off(ctx, "robinhood", FAKE, execute=True),
               lambda: tt.unquarantine(ctx, "robinhood", FAKE, execute=True)):
        ok, msg = fn()
        assert not ok and ("denied" in msg or "requires" in msg)
    assert token_pins.all_pins() == []
    assert op.get_position("rob", "robinhood", FAKE).status == "open"


def test_untrust_records_rejection_quarantines_and_the_gate_refuses():
    ok, msg = tt.untrust(_owner(), "robinhood", FAKE)
    assert ok, msg
    assert token_pins.rejection("robinhood", FAKE) is not None
    assert op.get_position("rob", "robinhood", FAKE).status == "quarantined"
    from tools.defi.identity_gate import buy_identity_refusal
    ident = types.SimpleNamespace(symbol="PNL", name="Pissin N Lying", verified=False,
                                  source="frozen")
    ctx = types.SimpleNamespace(user_id="rob", metadata={}, role="orchestrator",
                                is_sub_agent=False)
    why = buy_identity_refusal(chain="robinhood", token_out=FAKE, id_out=ident,
                               max_spend_usd=1.0, route_verdict="AGREES",
                               execution_context=ctx)
    assert why and "NOT trusted" in why and "polyrob" not in why


def test_trust_clears_a_rejection_and_untrust_drops_a_pin():
    tt.untrust(_owner(), "robinhood", FAKE, symbol="PNL")
    tt.trust(_owner(), "robinhood", FAKE, "PNL")
    assert token_pins.rejection("robinhood", FAKE) is None
    tt.untrust(_owner(), "robinhood", FAKE)
    assert token_pins.owner_pin("robinhood", FAKE) is None


def test_write_off_confirms_then_books_the_cost_as_loss():
    ok, preview = tt.write_off(_owner(), "robinhood", FAKE)
    assert ok and "$134.54" in preview and "go" in preview
    assert op.get_position("rob", "robinhood", FAKE).status == "open"
    ok, done = tt.write_off(_owner(), "robinhood", FAKE, reason="airdropped look-alike",
                            execute=True)
    assert ok, done
    pos = op.get_position("rob", "robinhood", FAKE)
    assert pos.status == "written_off" and "134.54" in pos.status_reason
    assert pos.entry_usd == pytest.approx(134.54)  # the cost basis is kept


def test_unquarantine_lifts_the_status_and_the_rejection():
    tt.untrust(_owner(), "robinhood", FAKE)
    ok, _ = tt.unquarantine(_owner(), "robinhood", FAKE, execute=True)
    assert ok
    assert op.get_position("rob", "robinhood", FAKE).status == "open"
    assert token_pins.rejection("robinhood", FAKE) is None


def test_trust_view_lists_every_source_and_the_quarantined_holding():
    tp.record_own_token("robinhood", REAL, kind="launchpad_launch")
    token_pins.pin("base", "0x" + "1" * 40, "ABC")
    tt.untrust(_owner(), "robinhood", FAKE)
    view = tt.trust_view("rob")
    sources = {t["source"] for t in view["trusted"]}
    assert {"own_launch", "owner_pin"} <= sources
    assert view["holdings"][0]["status"] == "quarantined"
    text = tt.render_trust_view(view)
    assert "our own launch" in text and "/unquarantine robinhood" in text
    assert "polyrob" not in text


def test_a_pre_w1_pin_store_reads_owner_pin(tmp_path, _stores):
    import os
    os.makedirs(os.path.dirname(_stores), exist_ok=True)
    conn = sqlite3.connect(_stores)
    conn.execute("CREATE TABLE token_pins (chain TEXT NOT NULL, address TEXT NOT NULL, "
                 "symbol TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', pinned_ts REAL "
                 "NOT NULL, PRIMARY KEY (chain, address))")
    conn.execute("INSERT INTO token_pins VALUES ('robinhood', ?, 'PNL', '', 1.0)", (REAL,))
    conn.commit()
    conn.close()
    assert token_pins.owner_pin("robinhood", REAL)["source"] == "owner_pin"
    assert token_pins.all_rejections() == []
    tt.trust(_owner(), "base", "0x" + "2" * 40, "XYZ")  # the first write migrates
    assert token_pins.owner_pin("base", "0x" + "2" * 40)["source"] == "owner_approved"


def test_wallet_tokens_reply_needs_go_to_write():
    text = tt.tokens_reply("rob", ["trust", "robinhood", REAL, "PNL"])
    assert "Confirm" in text and token_pins.all_pins() == []
    text = tt.tokens_reply("rob", ["trust", "robinhood", REAL, "PNL", "go"])
    assert "Trusted" in text
    assert "Tokens I trust" in tt.tokens_reply("rob", ["tokens"])


@pytest.mark.parametrize("verb", ["trust", "untrust"])
def test_an_unknown_chain_name_writes_nothing(verb):
    """Validation 2026-09-27: `/wallet trust eth 0x… SYM go` replied '✅ Trusted' and
    pinned under 'eth' — a key no gate reads (the gates use 'ethereum')."""
    addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    fn = getattr(tt, verb)
    ok, msg = fn(_owner(), "eth", addr, symbol="USDC")
    assert ok is False
    assert "ethereum" in msg
