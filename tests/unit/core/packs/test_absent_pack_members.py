"""067 P3: a named profile may list a pack's tool ids (anysite, perplexity,
twitter, x_browser, and the markets pack's polymarket_data / hyperliquid_data);
with the pack absent the session lists drop them quietly and core still boots.
The X pack's surface row and cron channel are absent too (067 P3b); the venue
tools, their descriptors and their approval rows are absent while the reserved
correspondent tokens still refuse (067 P4).

Fresh interpreter, no ``polyrob.packs`` entry point visible (= the pack is not
installed), whatever this machine has installed."""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]

_PROBE = """
import json, logging
warned = []
class H(logging.Handler):
    def emit(self, r):
        if r.levelno >= logging.WARNING:
            warned.append(r.getMessage())
logging.getLogger().addHandler(H())
import core.packs.loader as L
L._entry_points = lambda: []
L.register_policies()
L.load_packs()
from core.status_packs import packs_section
from cli.toolset import resolve_tool_list
from agents.task import tool_defaults as td
from agents.task import constants as c
from core.config_policy import rigs
from core.config_policy.profiles import PROFILES
from core.bootstrap import cli_unavailable_tools
lists = {
    "cli_default": resolve_tool_list(None, None)[0],
    "cli_research": resolve_tool_list(None, "research")[0],
    "server_default": td.server_default_tools(),
    "session_default": td.default_session_tools(),
    "toolset_research": td.resolve_toolset("research"),
    "rig_research": rigs.rig_tools("research"),
    "grant": list(c.autonomous_mode_tools()),
    "social": td.resolve_toolset("social"),
    "owner_interactive": td.resolve_toolset("owner_interactive"),
    "trading_research": td.resolve_toolset("trading_research"),
}
from tools.descriptors import TOOL_DESCRIPTORS
from core.config_policy.payment_tools import PAYMENT_APPROVAL_TOOLS
from agents.task.agent.core.correspondent_gate import is_high_impact
venue = {"descriptors": sorted(t for t in TOOL_DESCRIPTORS if "market" in t or "liquid" in t),
         "approval": sorted(v for v in PAYMENT_APPROVAL_TOOLS if "market" in v or "liquid" in v),
         "reserved": [is_high_impact(t) for t in ("hyperliquid", "polymarket")]}
from core.surfaces import catalog
from core.delivery_channels import unavailable_reason
from cron.delivery import allowed_targets
print(json.dumps({"lists": lists, "raw": list(PROFILES["toolset:research"]),
                  "packs": packs_section().lines if hasattr(packs_section(), "lines") else str(packs_section()),
                  "surfaces": list(catalog.surface_ids()),
                  "targets": list(allowed_targets()),
                  "twitter_channel": unavailable_reason("twitter"),
                  "venue": venue,
                  "warned": [w for w in warned if any(t in w for t in
                             ("anysite", "perplexity", "twitter", "x_browser",
                              "polymarket", "hyperliquid"))]}))
"""


def test_core_boots_and_every_default_list_drops_absent_pack_ids():
    env = {k: v for k, v in os.environ.items()
           if k not in ("POLYROB_PACKS", "POLYROB_PACKS_DISABLED", "AUTONOMY_MODE")}
    env["POLYROB_ENV_KEY_BACKFILL"] = "0"
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=str(REPO), env=env,
                         capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stderr[-3000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    for name, ids in got["lists"].items():
        for pack_tool in ("anysite", "perplexity", "twitter", "x_browser", "polymarket",
                          "polymarket_data", "hyperliquid", "hyperliquid_data"):
            assert pack_tool not in ids, (name, ids)
    assert "x" not in got["surfaces"] and "twitter" not in got["targets"]
    assert "no installed pack provides" in got["twitter_channel"]
    assert "anysite" in got["raw"], "the profile NAME keeps the pack's ids"
    assert "polymarket_data" in got["raw"]
    assert got["venue"] == {"descriptors": [], "approval": [], "reserved": [True, True]}
    assert got["warned"] == []
    assert "no packs installed" in json.dumps(got["packs"])
