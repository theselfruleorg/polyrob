"""P7 finalization: construction-param validation extracted from the ~1000-line
AgentConstructionMixin.__init__ into a pure, testable static method."""
import pytest

from agents.task.agent.core.construction import AgentConstructionMixin
from core.exceptions import ValidationError as ROBValidationError

_v = AgentConstructionMixin._validate_construction_params


def _ok(**over):
    kw = dict(max_failures=3, retry_delay=1, max_input_tokens=1000,
              max_actions_per_step=10, max_error_length=400, task="do it",
              orchestrator=object())
    kw.update(over)
    return kw


def test_valid_params_pass():
    _v(**_ok())  # no raise


@pytest.mark.parametrize("over", [
    {"max_failures": -1}, {"retry_delay": -1}, {"max_input_tokens": 0},
    {"max_actions_per_step": 0}, {"max_error_length": 0},
    {"task": ""}, {"task": None}, {"orchestrator": None},
])
def test_invalid_params_raise(over):
    with pytest.raises(ROBValidationError):
        _v(**_ok(**over))


def test_none_max_input_tokens_is_allowed():
    _v(**_ok(max_input_tokens=None))  # None is valid (auto-calculated later)


def test_memory_profiling_is_noop_when_flag_off(monkeypatch):
    """_setup_memory_profiling (extracted from __init__) must be a no-op when
    PROFILE_MEM != '1' and never raise."""
    import types
    monkeypatch.delenv("PROFILE_MEM", raising=False)
    obj = types.SimpleNamespace()
    import logging
    obj.logger = logging.getLogger("t")
    obj._setup_memory_profiling = types.MethodType(
        AgentConstructionMixin._setup_memory_profiling, obj
    )
    obj._setup_memory_profiling()  # no raise, no side effect


def test_normalize_save_conversation_path_none():
    """_normalize_save_conversation_path (extracted) sets None when unset."""
    import types
    obj = types.SimpleNamespace()
    obj._normalize_save_conversation_path = types.MethodType(
        AgentConstructionMixin._normalize_save_conversation_path, obj
    )
    obj._normalize_save_conversation_path(None)
    assert obj.save_conversation_path is None
