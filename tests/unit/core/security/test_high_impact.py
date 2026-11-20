"""The high-impact classification lives in core (proposal 125 S21f).

``core.security.high_impact`` owns the name layer, the tool-id layer, the
prefixes and the verb substrings; the correspondent gate re-exports the same
objects so the room gate and the taint gate share one policy.
"""
import importlib
import subprocess
import sys


def test_core_owns_the_classification():
    hi = importlib.import_module("core.security.high_impact")
    assert hi.is_high_impact("send_email")
    assert hi.is_high_impact("hyperliquid_place_limit_order")
    assert not hi.is_high_impact("get_trade_history")
    assert not hi.is_high_impact(None)
    assert hi.is_high_impact_call("anything", "code_execution") is (
        "code_execution" in hi.HIGH_IMPACT_TOOL_IDS)
    assert hi.HIGH_IMPACT_TOOLS is hi._HIGH_IMPACT_NAMES


def test_the_classification_has_one_home():
    """The correspondent gate reads core.security.high_impact; it re-exports nothing."""
    from agents.task.agent.core import correspondent_gate as cg
    for name in ("_HIGH_IMPACT_NAMES", "HIGH_IMPACT_TOOLS", "HIGH_IMPACT_TOOL_IDS",
                 "_HIGH_IMPACT_PREFIXES", "_HIGH_IMPACT_VERB_SUBSTRINGS", "is_high_impact"):
        assert not hasattr(cg, name), name


def test_high_impact_imports_no_agents_module():
    body = ("import sys, core.security.high_impact as h; h.is_high_impact_call('x', 'y'); "
            "print(sorted(m for m in sys.modules if m.startswith(('agents', 'tools'))))")
    out = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "[]"
