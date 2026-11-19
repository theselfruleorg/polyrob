"""121: the one rule for a model the agent chooses — no price gate; the owner's switch only."""
import pytest

from core import model_choice as mc, model_pins

CHEAP, DEAR = (0.045, 0.5), (15.0, 75.0)


@pytest.mark.parametrize("kw,verdict,why", [
    (dict(listed=True, mode="free", new_price=DEAR), mc.ALLOW, ""),
    (dict(listed=True, mode="listed", new_price=None), mc.ALLOW, ""),
    (dict(listed=False, mode="free", new_price=CHEAP), mc.ALLOW, ""),
    (dict(listed=False, mode="free", new_price=DEAR), mc.ALLOW, ""),        # no price gate
    (dict(listed=False, mode="free", new_price=None), mc.ALLOW, mc.NO_PRICE_WARNING),
])
def test_any_model_runs(kw, verdict, why):
    assert mc.decide(**kw) == (verdict, why)


def test_the_owners_listed_switch_is_the_only_refusal():
    got, why = mc.decide(listed=False, mode="listed", new_price=CHEAP)
    assert got == mc.REFUSE and "listed" in why


def test_the_owners_switch(tmp_path):
    assert mc.choice_mode("u1", tmp_path) == "free"            # nothing set
    assert mc.choice_mode(None, tmp_path) == "free"
    assert model_pins.set_choice("u1", "listed", home_dir=tmp_path)[0]
    assert mc.choice_mode("u1", tmp_path) == "listed"
    assert model_pins.set_choice("u1", "nope", home_dir=tmp_path)[0] is False
    assert model_pins.set_choice("u1", "free", home_dir=tmp_path)[0]
    assert mc.choice_mode("u1", tmp_path) == "free"
    # an unreadable store is the narrow answer, never "free"
    path = model_pins._path("u1", tmp_path)
    path.write_text("{not json", encoding="utf-8")
    assert mc.choice_mode("u1", tmp_path) == "listed"
