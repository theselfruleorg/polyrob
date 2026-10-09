"""Publishing & app-deployment evaluation 2026-09-05 (Wave 1).

On prod every `tool_timeout` (x15) was `shell_run` / `code_execution_run_code` at
`timeout_s: 60` — the CONTROLLER's per-action default for a tool with no row in
`TimeoutConfig.TOOL_TIMEOUTS`, so the tools' own foreground ceilings never got a
chance. The two build tools need their own cap, sitting ABOVE the foreground
ceiling so the tool's own clean kill (process-group, with output) fires first.
"""
from agents.task.constants import TimeoutConfig
from tools.code_exec.limits import dev_exec_max_timeout_sec


def test_build_tools_have_their_own_controller_cap():
    for tool in ("shell", "code_execution"):
        assert tool in TimeoutConfig.TOOL_TIMEOUTS, tool
        assert TimeoutConfig.get_tool_timeout(tool) > dev_exec_max_timeout_sec(), tool


def test_unknown_tool_still_gets_default():
    assert TimeoutConfig.get_tool_timeout("no-such-tool") == \
        TimeoutConfig.TOOL_TIMEOUTS["default"]


def test_dev_exec_ceiling_default_and_env(monkeypatch):
    monkeypatch.delenv("SHELL_MAX_TIMEOUT_SEC", raising=False)
    assert dev_exec_max_timeout_sec() == 600.0  # 073 W3: was 300
    monkeypatch.setenv("SHELL_MAX_TIMEOUT_SEC", "90")
    assert dev_exec_max_timeout_sec() == 90.0
    monkeypatch.setenv("SHELL_MAX_TIMEOUT_SEC", "garbage")
    assert dev_exec_max_timeout_sec() == 600.0
    monkeypatch.setenv("SHELL_MAX_TIMEOUT_SEC", "0")
    assert dev_exec_max_timeout_sec() == 1.0


def test_default_tool_timeout_is_180s_and_below_the_step_timeout(monkeypatch):
    """Prod 2026-10-02..09: 60 s cut 24 reconcile runs and 9 X posts mid-flight.

    The default is 180 s (MCP's figure) and must stay below the 300 s step
    timeout so a hung action is still cut by its own guard first.
    """
    import os
    if "DEFAULT_TOOL_TIMEOUT_SECONDS" in os.environ:
        import pytest
        pytest.skip("env override set")
    assert TimeoutConfig.TOOL_TIMEOUTS["default"] == 180
    assert TimeoutConfig.TOOL_TIMEOUTS["default"] < TimeoutConfig.STEP_TIMEOUT
