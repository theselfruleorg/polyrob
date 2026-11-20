"""A-14: the autonomous tool grant lives in core; ``agents.task.constants`` only
re-exports it, and the status rails read the one switch instead of a copy."""
import pathlib

import agents.task.constants as constants
from core.config_policy import autonomous_tools as at

REPO = pathlib.Path(__file__).resolve().parents[4]


def test_constants_reexports_the_same_objects():
    for name in ("AUTONOMOUS_MODE_TOOLS", "DEFI_AUTONOMOUS_TOOLS", "X402_AUTONOMOUS_TOOLS",
                 "DEFI_LAUNCH_AUTONOMOUS_TOOLS", "AGENT_NFT_AUTONOMOUS_TOOLS",
                 "autonomous_mode_tools", "stable_autonomous_toolset",
                 "effective_autonomous_tools"):
        assert getattr(constants, name) is getattr(at, name), name


def test_the_module_defines_the_rule_and_constants_does_not():
    text = (REPO / "agents/task/constants.py").read_text()
    assert "def effective_autonomous_tools" not in text
    assert "def autonomous_mode_tools" not in text


def test_status_rails_has_no_copy_of_the_toolset_default():
    text = (REPO / "core/status_rails.py").read_text()
    assert '"STABLE_AUTONOMOUS_TOOLSET"' not in text
    assert "stable_autonomous_toolset" in text
