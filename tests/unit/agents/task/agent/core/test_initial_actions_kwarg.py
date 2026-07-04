"""Regression: run_loop must call multi_act with the correct kwarg name.

multi_act's parameter is `_page_extraction_llm` (no **kwargs); run_loop.py
previously passed `page_extraction_llm=`, raising TypeError that the surrounding
try/except swallowed — so configured initial_actions were silently dropped.
This test pins the parameter name so the two stay in sync.
"""
import inspect

from tools.controller.execution import ExecutionMixin


def test_multi_act_has_underscore_page_extraction_llm_param():
    sig = inspect.signature(ExecutionMixin.multi_act)
    assert "_page_extraction_llm" in sig.parameters
    # And there is no **kwargs to absorb a mis-named kwarg.
    assert not any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def test_run_loop_passes_underscore_prefixed_kwarg():
    src = inspect.getsource(
        __import__("agents.task.agent.core.run_loop", fromlist=["x"])
    )
    # The initial-actions call site must use the underscore-prefixed name.
    assert "_page_extraction_llm=self.page_extraction_llm" in src
    assert "\t\t\t\t\t\tpage_extraction_llm=self.page_extraction_llm" not in src
