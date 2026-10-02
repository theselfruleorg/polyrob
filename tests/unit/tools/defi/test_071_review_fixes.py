"""071 final review: the confirmed bugs stay fixed."""
import sqlite3

from core import open_positions as op
from core.open_positions import PositionEntry
from tools.defi import reconcile as rec
from tools.defi.positions_view import figures

TOK = "0x" + "ab" * 20


def _held(**kw):
    base = dict(address=TOK, symbol="MEME", qty=1_000_000.0, raw_units=10 ** 24,
                value_usd=None, balance_known=True)
    base.update(kw)
    return rec.ChainHolding(**base)


def _verdict(holding):
    return rec.diff([], [holding], rail=[]).verdict


# -- reconcile: "unpriced" is no longer one bucket ---------------------------

def test_a_token_nothing_trades_is_unsolicited_and_clean():
    assert _verdict(_held(price_state="no_pool")) == "CLEAN"


def test_a_low_confidence_holding_worth_more_than_dust_is_a_disagreement():
    """The false CLEAN: an unrecorded thin-pool buy used to read as an airdrop."""
    assert _verdict(_held(price_state="priced", est_value_usd=250.0)) == "DISAGREEMENT"


def test_low_confidence_dust_stays_clean():
    assert _verdict(_held(price_state="priced", est_value_usd=0.02)) == "CLEAN"


def test_a_price_outage_is_unverified_not_clean():
    assert _verdict(_held(price_state="failed")) == "UNVERIFIED"


# -- open_positions: a pre-071 $0 trade basis is unknown ----------------------

def test_a_zero_trade_basis_becomes_unknown():
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    conn.execute(op._TABLE)
    conn.execute("INSERT INTO open_positions (user_id, chain, address, qty, entry_usd, "
                 "entry_ts, updated_ts, origin) VALUES ('u','base',?,5,0,1,1,'trade')", (TOK,))
    conn.execute("INSERT INTO open_positions (user_id, chain, address, qty, entry_usd, "
                 "entry_ts, updated_ts, origin) VALUES ('u','eth',?,5,12.5,1,1,'trade')", (TOK,))
    op._zero_basis_is_unknown(conn)
    rows = dict(conn.execute("SELECT chain, entry_usd FROM open_positions").fetchall())
    assert rows == {"base": None, "eth": 12.5}
    op._zero_basis_is_unknown(conn)          # idempotent


# -- positions: no figures on a disputed price --------------------------------

def test_a_disputed_price_computes_no_value_or_pnl():
    entry = PositionEntry(chain="base", address=TOK, symbol="MEME", qty=10.0,
                          entry_usd=5.0, entry_ts=1.0)
    price = type("Q", (), {"price_usd": 2.0, "confidence": "disputed"})()
    f = figures(entry, price)
    assert f.value_usd is None and f.unrealized_usd is None
    low = type("Q", (), {"price_usd": 2.0, "confidence": "low"})()
    assert figures(entry, low).value_usd == 20.0      # low is shown, flagged
