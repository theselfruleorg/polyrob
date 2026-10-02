"""W1: /writeoff, /unquarantine and /wallet tokens|trust|untrust from the phone.

The seat is reach, never policy: the confirm step and the owner-turn gate live
in ``core.wallet.token_trust``; these tests prove the Telegram and REPL seats
reach the SAME functions and that nothing writes without ``go``.
"""
import types

import pytest

from core import open_positions as op
from core.wallet import token_pins

FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"
REAL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")
    pins = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: pins)
    op.apply_delta("rob", op.PositionDelta(chain="robinhood", address=FAKE.lower(),
                                           symbol="PNL", qty=1000.0, cost_usd=134.54),
                   db_path=str(tmp_path / "open_positions.db"))
    return tmp_path


@pytest.mark.asyncio
async def test_writeoff_previews_then_records(_stores):
    from surfaces.telegram.holdings_ops import writeoff_verb
    data_dir = str(_stores)
    text = await writeoff_verb(user_id="rob", data_dir=data_dir,
                               args=["robinhood", FAKE])
    assert "Realized loss: $134.54" in text and "/writeoff robinhood" in text
    db = str(_stores / "open_positions.db")
    assert op.get_position("rob", "robinhood", FAKE, db_path=db).status == "open"
    text = await writeoff_verb(user_id="rob", data_dir=data_dir,
                               args=["robinhood", FAKE, "go", "airdropped", "look-alike"])
    assert text.startswith("✅ Written off")
    pos = op.get_position("rob", "robinhood", FAKE, db_path=db)
    assert pos.status == "written_off" and "airdropped look-alike" in pos.status_reason


@pytest.mark.asyncio
async def test_unquarantine_undoes_a_quarantine(_stores):
    from surfaces.telegram.holdings_ops import unquarantine_verb
    db = str(_stores / "open_positions.db")
    op.set_status("rob", "robinhood", FAKE, "quarantined", reason="look-alike", db_path=db)
    text = await unquarantine_verb(user_id="rob", data_dir=str(_stores),
                                   args=["robinhood", FAKE])
    assert "Confirm" in text
    text = await unquarantine_verb(user_id="rob", data_dir=str(_stores),
                                   args=["robinhood", FAKE, "go"])
    assert "open again" in text
    assert op.get_position("rob", "robinhood", FAKE, db_path=db).status == "open"


def test_wallet_tokens_and_trust_ride_the_wallet_verb(_stores, monkeypatch):
    from surfaces.telegram import owner_ops
    monkeypatch.setattr("core.wallet.authority.owner_refusal", lambda uid: None)
    text = owner_ops.wallet_reply(["tokens"], user_id="rob", data_dir=str(_stores))
    assert "Tokens I trust" in text
    text = owner_ops.wallet_reply(["trust", "robinhood", REAL, "PNL"], user_id="rob",
                                  data_dir=str(_stores))
    assert "Confirm" in text and token_pins.all_pins() == []
    owner_ops.wallet_reply(["trust", "robinhood", REAL, "PNL", "go"], user_id="rob",
                           data_dir=str(_stores))
    assert token_pins.owner_pin("robinhood", REAL)["source"] == "owner_approved"


@pytest.mark.asyncio
async def test_the_repl_twin_says_the_same_thing(_stores, monkeypatch):
    from cli.ui.commands import h_holdings
    monkeypatch.setattr("cli.ui.commands.h_owner._tenant", lambda ctx: "rob")
    monkeypatch.setattr("cli.ui.commands.h_owner._admin_data_dir",
                        lambda ctx=None, write=None: str(_stores))
    out = []
    ctx = types.SimpleNamespace(args=["robinhood", FAKE],
                                emit=lambda text, title=None: out.append(text))
    await h_holdings.h_writeoff(ctx)
    assert "Realized loss: $134.54" in out[0]
