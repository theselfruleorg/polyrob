"""086 W5: the intake card's words and echo (core/trading_intake.py)."""
import pytest

from core import trading_intake as ti
from core import trading_mandate as tm


def test_parse_bare_and_keyed_words():
    assert ti.parse_start_args(["$150", "robinhood+solana", "memecoin"]) == {
        "budget_usd": 150.0, "preset": "memecoin", "chains": ["robinhood", "solana"]}
    got = ti.parse_start_args(["budget=1,000", "chains=base", "ticket=5", "slots=3",
                               "daily_loss_pct=8"])
    assert got == {"budget_usd": 1000.0, "chains": ["base"], "ticket_usd": 5.0, "slots": 3,
                   "breakers": {"daily_loss_pct": 8.0}}
    for bad in (["colour=red"], ["ticket=abc"], ["150", "200"]):
        with pytest.raises(tm.MandateError):
            ti.parse_start_args(bad)


def test_the_owner_tunes_a_breaker_and_the_echo_shows_it():
    m = tm.draft("u", budget_usd=150, chains=["solana"], breakers={"daily_loss_pct": 8})
    assert "daily stop at -8% of the allocation" in ti.echo_line(m)


def test_echo_for_majors_and_dca():
    majors = ti.echo_line(tm.draft("u", budget_usd=1000, chains=["base"], preset="majors"))
    assert "-8% stop" in majors and "$100 tickets" in majors
    assert "no exit barriers" in ti.echo_line(tm.draft("u", budget_usd=100, chains=["base"],
                                                         preset="dca"))


def test_a_draft_needs_an_allocation_and_a_chain(tmp_path):
    p = str(tmp_path / "trading.db")
    with pytest.raises(tm.MandateError):
        ti.save_draft("u", {"chains": ["base"]}, origin="owner", path=p)
    with pytest.raises(tm.MandateError):
        ti.save_draft("u", {"budget_usd": 10, "chains": []}, origin="owner", path=p)
    ref, m = ti.save_draft("u", {"budget_usd": 10, "chains": ["base"]}, origin="owner", path=p)
    assert ti.load_draft(ref, user_id="u", path=p)["budget_usd"] == 10
    assert tm.get_active("u", path=p) is None       # the store is whole, and empty
