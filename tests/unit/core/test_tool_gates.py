"""067 P0.11: tool enable gates reach core/agents through core.tool_gates."""

import subprocess
import sys
from unittest import mock

from core import tool_gates


def test_anysite_gate_is_registered_after_import_tools():
    # Fresh interpreter: nothing but ``import tools`` may be needed.
    code = (
        "import tools\n"
        "from core.tool_gates import gate_for\n"
        "assert gate_for('hf_deploy') is not None\n"
        "from core.boot_reconcilers import boot_reconciler\n"
        "assert boot_reconciler('hf_deploy') is not None\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_unregistered_or_raising_gate_is_off(monkeypatch):
    monkeypatch.setattr(tool_gates, "_GATES", {})
    assert tool_gates.gate_for("anysite") is None
    assert tool_gates.gate_on("anysite") is False

    def boom():
        raise RuntimeError("x")

    tool_gates.register_gate("anysite", boom)
    assert tool_gates.gate_on("anysite") is False


def test_cli_tool_list_sees_the_anysite_gate_in_a_fresh_process():
    code = (
        "import os\n"
        "os.environ.pop('ANYSITE_TOOL_ENABLED', None)\n"
        "os.environ.pop('POLYROB_AGENT_TOOLSET', None)\n"
        "from cli.toolset import resolve_tool_list\n"
        "tools, _ = resolve_tool_list(None, None)\n"
        "assert 'anysite' in tools, tools\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# --- 067 P1: the static gate column on the per-tool rows ---------------------------

def test_gate_views_are_derived_from_the_rows():
    from agents.task.agent.core.tool_availability import GATED_TOOL_REGISTRY
    from core.tool_capabilities import TOOL_CAPABILITIES, row_field
    from tools.controller.tool_load_report import TOOL_GATE_FLAGS

    flags, disclosed = {}, {}
    for tid in TOOL_CAPABILITIES:
        gate = row_field(tid, "gate")
        if gate is not None and gate.flag:
            flags[tid] = gate.flag
        if gate is not None and gate.tier:
            disclosed[tid] = (gate.label, gate.tier, gate.remedy)
    assert TOOL_GATE_FLAGS == flags
    assert GATED_TOOL_REGISTRY == disclosed


def test_the_gate_store_is_one_module():
    from core import tool_capabilities as tc
    assert tc.register_gate is tool_gates.register_gate
    assert tc.gate_for is tool_gates.gate_for
    assert tc.gate_on is tool_gates.gate_on


def test_toolgate_validates_its_disclosure():
    import pytest

    from core.tool_gates import ToolGate
    assert ToolGate(flag="X_ENABLED").disclosure() is None
    assert ToolGate(label="l", tier="reserved", remedy="r").disclosure() == ("l", "reserved", "r")
    with pytest.raises(ValueError):
        ToolGate(tier="bogus", label="l", remedy="r")
    with pytest.raises(ValueError):
        ToolGate(tier="disabled")
