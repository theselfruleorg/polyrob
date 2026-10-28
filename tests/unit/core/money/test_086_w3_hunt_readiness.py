"""086 W3: the hunt's $0 skip signal shares the entry step's rules (one rule set)."""
import pytest

from core import trading_mandate as tm
from core.money.mandate_gate import hunt_refusal
from core.open_positions import PositionDelta, apply_delta

MEME = "0xb200000000000000000000ea8625786a776539fb"
MEME2 = "0xb200000000000000000000b344cb4a1e8bd51968"
T0 = 1_000_000.0


@pytest.fixture
def paths(tmp_path):
    return str(tmp_path / "trading.db"), str(tmp_path / "open_positions.db")


@pytest.fixture
def mandate(paths):
    return tm.create("u1", actor=tm.OWNER_SEAT, budget_usd=10.0, chains=["base", "solana"],
                     slots=2, path=paths[0], now=T0)


def _buy(book, token, cost, ts=T0 + 10):
    apply_delta("u1", PositionDelta(chain="base", address=token, symbol="X", qty=100.0,
                                    cost_usd=cost), db_path=book, now=ts)


def _ready(mandate, paths, user="u1", now=T0 + 100):
    return hunt_refusal(mandate.id, user_id=user, now=now, path=paths[0], book_path=paths[1])


def test_an_active_mandate_with_room_is_ready(mandate, paths):
    r = _ready(mandate, paths)
    assert r.ready is True and r.held == () and r.chains == ("base", "solana")


def test_held_rows_are_named_for_the_skip_list(mandate, paths):
    _buy(paths[1], MEME, 1.0)
    r = _ready(mandate, paths)
    assert r.ready is True and r.held == (("base", MEME),)


def test_full_slots_are_not_ready(mandate, paths):
    _buy(paths[1], MEME, 1.0)
    _buy(paths[1], MEME2, 1.0)
    r = _ready(mandate, paths)
    assert r.ready is False and "slots" in r.reason


def test_a_used_allocation_is_not_ready(paths):
    m = tm.create("u1", actor=tm.OWNER_SEAT, budget_usd=3.0, chains=["base"], slots=5,
                  path=paths[0], now=T0)
    _buy(paths[1], MEME, 2.5)
    r = hunt_refusal(m.id, user_id="u1", now=T0 + 100, path=paths[0], book_path=paths[1])
    assert r.ready is False and "allocation" in r.reason


@pytest.mark.parametrize("status", ["paused", "stopped"])
def test_a_mandate_that_is_not_active_is_not_ready(mandate, paths, status):
    tm.set_status(mandate.id, status, actor=tm.OWNER_SEAT, path=paths[0])
    assert _ready(mandate, paths).ready is False


def test_unknown_mandate_and_other_user_are_not_ready(mandate, paths):
    assert hunt_refusal("m_nope", user_id="u1", path=paths[0], book_path=paths[1]).ready is False
    assert _ready(mandate, paths, user="u2").ready is False


def test_an_unreadable_mandate_store_is_unknown_not_no(tmp_path, paths, mandate):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite file" * 100)
    r = hunt_refusal(mandate.id, user_id="u1", path=str(bad), book_path=paths[1])
    assert r.ready is None and "could not be read" in r.reason
