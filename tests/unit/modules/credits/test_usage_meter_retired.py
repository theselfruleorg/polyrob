"""C5: UsageMeter is DEPRECATED (modules/credits/usage_meter.py:1-37, explicit
double-billing warning in its own docstring) yet was still registered as a
service and consumed as a legacy fallback. Verified NOT live double-billing
(usage_tracker is always available whenever usage_meter would be — see the
plan doc) — this just retires dead wiring.
"""
import inspect

import core.initialization as initialization
import agents.task.agent.core.construction as construction
import agents.task.agent.core.run_loop as run_loop


def test_usage_meter_no_longer_registered():
    src = inspect.getsource(initialization)
    assert "register_service('usage_meter'" not in src
    assert "from modules.credits.usage_meter import UsageMeter" not in src


def test_construction_no_longer_fetches_usage_meter():
    src = inspect.getsource(construction)
    assert "get_service('usage_meter')" not in src


def test_run_loop_no_longer_falls_back_to_usage_meter():
    src = inspect.getsource(run_loop)
    assert "self.usage_meter" not in src
    assert "finalize_session_cost" not in src
