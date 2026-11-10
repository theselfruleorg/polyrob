"""112 W4: the `compute` action — hashes and the fair draw with no code exec."""
import asyncio
import hashlib

import pytest

import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
from tools.controller.service import Controller


@pytest.fixture(scope="module")
def controller():
    from tools.controller.compute_action import register_compute_action
    from tools.controller.registry.service import Registry
    c = object.__new__(Controller)
    c.registry = Registry()
    register_compute_action(c)
    return c


def _run(controller, **params):
    reg = controller.registry.registry.actions["compute"]
    model = reg.param_model(**params)
    return asyncio.run(reg.function(model))


def test_compute_is_registered_on_every_controller(controller):
    import inspect
    assert "compute" in controller.registry.registry.actions
    assert "register_compute_action(self)" in inspect.getsource(Controller.__init__)


def test_hash(controller):
    res = _run(controller, op="hash", algo="sha256", data="abc")
    assert res.error is None and hashlib.sha256(b"abc").hexdigest() in res.extracted_content


def test_fair_draw_matches_core(controller):
    from core.fair_draw import fair_draw
    seed = "0x" + "ab" * 32
    res = _run(controller, op="fair_draw", seed=seed, ids=["1", "2", "3", "10", "200"], n=3)
    assert ", ".join(fair_draw(seed, ["1", "2", "3", "10", "200"], 3)) in res.extracted_content


def test_refusals_are_plain(controller):
    assert "required" in _run(controller, op="hash").error
    assert "required" in _run(controller, op="fair_draw", seed="0x1").error
    assert "unique" in _run(controller, op="fair_draw", seed="0x1", ids=["a", "a"], n=1).error


def test_policy_row_is_a_silent_read():
    from core.verb_policy import policy_for
    assert policy_for("compute").effect == "none"
