"""The discovery pack end to end (067 P3a): installed -> loaded, its rows come
from pack.toml, its tools register through phase 2; disabled -> every default
list drops its ids quietly and an explicit request names the pack."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("polyrob_discovery")

REPO = Path(__file__).resolve().parents[3]


def test_the_pack_is_loaded_and_owns_its_rows():
    from core.packs import state
    from core.tool_capabilities import TOOL_PERMISSIONS, TOOL_CAPABILITIES
    from core.verb_policy import policy_for
    from tools.descriptors import get_tool_class
    rec = state.record("discovery")
    assert rec is not None and rec.status == state.LOADED, rec and rec.line()
    assert state.pack_of_tool("anysite") == "discovery"
    assert state.pack_of_tool("perplexity") == "discovery"
    assert TOOL_CAPABILITIES["anysite"].cli == "static"
    assert TOOL_CAPABILITIES["perplexity"].cli == "incompatible"
    assert TOOL_PERMISSIONS["anysite"] == ("network.read", "process.spawn")
    assert policy_for("anysite_api").effect == "network"
    for name in ("anysite_describe", "perplexity_analyze", "perplexity_sources"):
        assert policy_for(name) is not None, name
    assert get_tool_class("anysite").__module__ == "polyrob_discovery.anysite.tool"
    assert get_tool_class("perplexity").__module__ == "polyrob_discovery.perplexity"


def test_every_emitted_action_has_a_row():
    from core.packs import state
    from polyrob_discovery.anysite.tool import AnysiteTool
    from polyrob_discovery.perplexity import PerplexityTool
    for tool_id, cls in (("anysite", AnysiteTool), ("perplexity", PerplexityTool)):
        for action in cls.__dict__:
            if action.startswith(tool_id + "_") and callable(getattr(cls, action)):
                assert state.action_refusal(tool_id, action) is None, action


_DISABLED = """
import json
import core.packs.loader as L
L.register_policies(); L.load_packs()
from cli.toolset import resolve_tool_list
from agents.task import tool_defaults as td
from core.status_packs import packs_section
from tools.controller.tool_load_report import describe_missing_tool
print(json.dumps({
    "cli": resolve_tool_list(None, None)[0],
    "server": td.server_default_tools(),
    "research": td.resolve_toolset("research"),
    "packs": packs_section().lines,
    "explicit": describe_missing_tool("perplexity"),
}))
"""


def test_disabled_pack_drops_out_and_is_named():
    env = {**os.environ, "POLYROB_PACKS_DISABLED": "discovery", "POLYROB_ENV_KEY_BACKFILL": "0"}
    env.pop("POLYROB_PACKS", None)
    out = subprocess.run([sys.executable, "-c", _DISABLED], cwd=str(REPO), env=env,
                         capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stderr[-3000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    for key in ("cli", "server", "research"):
        assert "anysite" not in got[key] and "perplexity" not in got[key], (key, got[key])
    assert any("discovery" in line and "disabled" in line for line in got["packs"])
    assert "pack 'discovery'" in got["explicit"] and "disabled" in got["explicit"]
