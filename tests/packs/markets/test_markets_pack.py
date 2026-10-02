"""The markets pack end to end (067 P4): installed -> loaded, its rows come from
pack.toml (incl. the owner approval lanes and the correspondent blocks of every
money write), its four tools register through phase 2, its API routers mount
under /api/packs/markets; disabled -> every default list drops its ids quietly,
the status and an explicit request name the pack, and no route is mounted."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("polyrob_markets")

REPO = Path(__file__).resolve().parents[3]
TOOLS = ("polymarket", "polymarket_data", "hyperliquid", "hyperliquid_data")


def test_the_pack_is_loaded_and_owns_its_rows():
    from core.packs import state
    from core.tool_capabilities import TOOL_CAPABILITIES, TOOL_PERMISSIONS, row_field
    from core.verb_policy import policy_for
    from tools.descriptors import get_tool_class
    rec = state.record("markets")
    assert rec is not None and rec.status == state.LOADED, rec and rec.line()
    for tid in TOOLS:
        assert state.pack_of_tool(tid) == "markets", tid
        assert TOOL_CAPABILITIES[tid].cli == "incompatible", tid
    for venue in ("polymarket", "hyperliquid"):
        assert {"money", "delegate_blocked", "readable_while_tainted",
                "writes_money"} <= set(TOOL_CAPABILITIES[venue])
        assert row_field(venue, "gate").tier == "reserved"
        assert TOOL_PERMISSIONS[venue] == ("network.read", "trade.execute")
        assert TOOL_PERMISSIONS[f"{venue}_data"] == ("network.read",)
        assert not set(TOOL_CAPABILITIES[f"{venue}_data"]), "a read tool carries nothing"
    row = policy_for("hyperliquid_update_leverage")
    assert (row.tool, row.lane, row.approval_owner) == ("hyperliquid", "owner_always", "hook")
    assert policy_for("polymarket_data_search_markets").tool == "polymarket_data"
    assert get_tool_class("polymarket").__module__ == "polyrob_markets.polymarket.service"
    assert get_tool_class("hyperliquid_data").__module__ == "polyrob_markets.hyperliquid.service"


def test_every_emitted_action_has_a_row_naming_its_own_tool():
    from core.packs import state
    from core.verb_policy import policy_for
    from polyrob_markets.hyperliquid.service import HL_READ_ACTIONS, HyperliquidTool
    from polyrob_markets.polymarket.service import PM_READ_ACTIONS, PolymarketTool

    def actions(cls):   # the decorated BaseTool actions (what get_actions() emits)
        return {a for a in dir(cls) if hasattr(getattr(cls, a, None), "_description")}

    seen = 0
    # The *_data tools emit exactly their READ sets (get_actions filters to them).
    for tool_id, emitted in (("polymarket", actions(PolymarketTool)),
                             ("polymarket_data", PM_READ_ACTIONS),
                             ("hyperliquid", actions(HyperliquidTool)),
                             ("hyperliquid_data", HL_READ_ACTIONS)):
        for action in emitted:
            name = f"{tool_id}_{action}"
            assert state.action_refusal(tool_id, name) is None, name
            assert policy_for(name).tool == tool_id, name
            seen += 1
    assert seen == 22 + 15 + 18 + 11, seen


def test_every_money_write_has_owner_approval_and_the_correspondent_block():
    """The pack's copy of tests/unit/core/test_money_verb_registration.py: every
    spend-side venue verb is on the owner approval lane and every money verb is
    blocked while correspondent-tainted (by row AND by the predicate that runs)."""
    from agents.task.agent.core.correspondent_gate import is_high_impact
    from core.config_policy.payment_tools import PAYMENT_APPROVAL_TOOLS
    from core.money.classify import money_action
    from core.verb_policy import VERB_POLICY
    rows = [r for r in VERB_POLICY.values() if r.tool in ("polymarket", "hyperliquid")]
    money = [r for r in rows if r.effect == "money"]
    assert len(money) == 4 + 7, sorted(r.name for r in money)
    for r in money:
        assert r.correspondent_blocked and is_high_impact(r.name), r.name
        assert money_action(r.name), r.name
        if r.side == "spend":
            assert r.approval_owner == "hook" and r.name in PAYMENT_APPROVAL_TOOLS, r.name
    for r in VERB_POLICY.values():
        if r.tool in ("polymarket_data", "hyperliquid_data"):
            assert not money_action(r.name), r.name


def test_the_routers_mount_under_the_pack_prefix():
    from core.packs.loader import api_routers
    from core.packs.spec import resolve_ref
    refs = [ref for pid, ref in api_routers() if pid == "markets"]
    assert len(refs) == 2
    prefixes = sorted(resolve_ref(r).prefix for r in refs)
    assert prefixes == ["/hyperliquid", "/polymarket"]


def test_pack_skills_carry_their_rules_and_left_the_builtin_library():
    from agents.task.agent.skill_manager import SkillManager
    ids = {"polymarket-market-research", "polymarket-portfolio-review", "polymarket-trading",
           "hyperliquid-market-data", "hyperliquid-account-review", "hyperliquid-trading"}
    sm = SkillManager()
    sm._ensure_rules_loaded()
    assert ids <= sm._pack_rule_ids
    base = REPO / "data" / "prompts" / "skills"
    rules = json.loads((base / "rules.json").read_text())
    for sid in ids:
        assert sid not in rules and not (base / sid).exists(), sid
    # The safety doctrine also covers the on-chain treasury rail: it stays in core.
    assert "crypto-trading-safety" in rules and (base / "crypto-trading-safety").is_dir()


_DISABLED = """
import json, logging
import core.packs.loader as L
L.register_policies(); L.load_packs()
from agents.task import tool_defaults as td
from agents.task import constants as c
from core.status_packs import packs_section
from core.packs import state
from tools.controller.tool_load_report import describe_missing_tool
from tools.descriptors import TOOL_DESCRIPTORS
from fastapi import FastAPI
from api.pack_routes import mount_pack_routers
app = FastAPI()
mount_pack_routers(app, logging.getLogger("t"))
print(json.dumps({
    "server": td.server_default_tools(),
    "research": td.resolve_toolset("research"),
    "trading_research": td.resolve_toolset("trading_research"),
    "grant": list(c.autonomous_mode_tools()),
    "packs": packs_section().lines,
    "explicit": describe_missing_tool("polymarket"),
    "descriptors": sorted(t for t in TOOL_DESCRIPTORS if "market" in t or "liquid" in t),
    "routes": sorted(r.path for r in app.routes if "markets" in r.path),
    "record": state.record("markets").line(),
}))
"""


def test_disabled_pack_drops_out_and_is_named():
    env = {**os.environ, "POLYROB_PACKS_DISABLED": "markets", "POLYROB_ENV_KEY_BACKFILL": "0",
           "AUTONOMY_MODE": "autonomous"}
    env.pop("POLYROB_PACKS", None)
    out = subprocess.run([sys.executable, "-c", _DISABLED], cwd=str(REPO), env=env,
                         capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stderr[-3000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    for key in ("server", "research", "trading_research", "grant"):
        assert not set(TOOLS) & set(got[key]), (key, got[key])
    assert "perplexity" in got["research"] or "web_fetch" in got["research"]
    assert any(line.startswith("markets ") and "disabled" in line for line in got["packs"]), \
        got["packs"]
    assert "pack 'markets'" in got["explicit"] and "disabled" in got["explicit"]
    assert got["descriptors"] == []
    assert got["routes"] == []
    assert "POLYROB_PACKS_DISABLED" in got["record"]


def test_the_gated_trading_skills_stay_out_of_a_session_catalog_without_their_tool():
    """A pack skill is also discovered as an external skill; the catalog must not
    put back one the P1-1 gate withheld (auto_activate false, tool not loaded)."""
    from agents.task.agent.skill_manager import SkillManager
    sm = SkillManager()
    for tools, shown in (([], set()), (["polymarket_data"], set()),
                         (["polymarket"], {"polymarket-trading"}),
                         (["hyperliquid"], {"hyperliquid-trading"})):
        ids = {m.skill_id for m in sm.get_catalog_skills(tool_ids=tools, max_skills=500)}
        assert ids & {"polymarket-trading", "hyperliquid-trading"} == shown, (tools, ids)
    assert sm.may_load_skill("polymarket-trading", tool_ids=[]) is False
