"""Item 3 — tool registration gating, run_code action, and native-schema parity."""
import logging

import pytest

import agents.task.agent.service  # noqa: F401 — avoid controller import cycle
from tools.code_exec import register_code_exec_tool
from tools.code_exec.tool import CodeExecutionTool, RunCodeParams


# --- registration gating -----------------------------------------------------

def test_flag_off_not_registered(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_ENABLED", raising=False)
    from tools.descriptors import TOOL_DESCRIPTORS, get_tool_class
    TOOL_DESCRIPTORS.pop("code_execution", None)  # ensure clean
    assert register_code_exec_tool() is False
    assert get_tool_class("code_execution") is None


def test_flag_on_registers(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_ENABLED", "true")
    from tools.descriptors import TOOL_DESCRIPTORS, TOOL_COMPONENTS, get_tool_class
    try:
        assert register_code_exec_tool() is True
        assert get_tool_class("code_execution") is CodeExecutionTool
    finally:
        # Clean global state so other tests / default tool list are unaffected.
        TOOL_DESCRIPTORS.pop("code_execution", None)
        TOOL_COMPONENTS[:] = [(n, c) for (n, c) in TOOL_COMPONENTS if n != "code_execution"]


def test_code_execution_not_in_default_tools():
    from tools.descriptors import get_default_tools
    assert "code_execution" not in get_default_tools()


# --- run_code action ---------------------------------------------------------

@pytest.mark.asyncio
async def test_run_code_action_returns_output(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    tool = object.__new__(CodeExecutionTool)
    tool.logger = logging.getLogger("code-exec-test")
    tool._backend = None
    result = await tool.run_code(RunCodeParams(language="python", code="print(2 + 2)"))
    assert getattr(result, "error", None) in (None, "")
    assert "4" in result.extracted_content


@pytest.mark.asyncio
async def test_run_code_action_reports_failure(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    tool = object.__new__(CodeExecutionTool)
    tool.logger = logging.getLogger("code-exec-test")
    tool._backend = None
    result = await tool.run_code(RunCodeParams(language="python", code="import sys; sys.exit(3)"))
    assert result.error and "status 3" in result.error


# --- native-schema parity (the seam the provider-checklist warns about) ------

def _action_for_run_code():
    from tools.controller.registry.views import RegisteredAction
    return RegisteredAction(
        name="run_code",
        description="Execute code",
        function=lambda **kw: None,
        param_model=RunCodeParams,
        tool="code_execution",
    )


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_run_code_schema_valid_per_provider(provider):
    from tools.controller.registry.schema_generators import get_schema_generator
    gen = get_schema_generator(provider)
    schema = gen.generate_tool_schema(_action_for_run_code())
    assert isinstance(schema, dict)
    # name present in provider-specific location
    blob = str(schema)
    assert "run_code" in blob
    assert "language" in blob and "code" in blob


def test_denylist_blocks_run_code():
    """POLYROB_TOOL_DENYLIST=run_code blocks the action via the generic pre-hook."""
    from tools.controller.service import make_denylist_hook
    hook = make_denylist_hook(["run_code"])
    assert hook("run_code", {}, None)  # truthy denial reason
    assert hook("read_file", {}, None) is None  # others unaffected
