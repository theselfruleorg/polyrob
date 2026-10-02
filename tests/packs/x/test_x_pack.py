"""The X pack end to end (067 P3b): installed -> loaded, its rows come from
pack.toml, its tools register through phase 2, its surface row and its cron
channel are live; disabled -> every default list drops its ids quietly, the X
surface and the ``twitter`` cron channel are absent with a named reason."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("polyrob_x")

REPO = Path(__file__).resolve().parents[3]


def test_the_pack_is_loaded_and_owns_its_rows():
    from core.packs import state
    from core.surfaces import catalog
    from core.tool_capabilities import TOOL_CAPABILITIES, TOOL_PERMISSIONS
    from core.tool_gates import gate_for
    from core.verb_policy import policy_for
    from tools.descriptors import get_tool_class
    rec = state.record("x")
    assert rec is not None and rec.status == state.LOADED, rec and rec.line()
    assert state.pack_of_tool("twitter") == "x" and state.pack_of_tool("x_browser") == "x"
    assert TOOL_CAPABILITIES["twitter"].cli == "static"
    assert TOOL_CAPABILITIES["x_browser"].cli == "static"
    assert "delegate_blocked" in TOOL_CAPABILITIES["x_browser"]
    assert TOOL_PERMISSIONS["twitter"] == ("network.read", "network.write", "social.post")
    assert policy_for("twitter_dm").effect == "comms"
    assert policy_for("x_browser_x_signup_start").approval == frozenset({"always_queued"})
    assert gate_for("twitter") is not None and gate_for("x_browser") is not None
    assert get_tool_class("twitter").__module__ == "polyrob_x.twitter_tool"
    row = catalog.get("x")
    assert row is not None and row.pack == "x" and not row.alias_owner and not row.forgeable
    assert row.module == "polyrob_x.surface" and "x_dedup.db" in catalog.state_dbs()
    assert set(rec.commands) == {"x", "x-account"}


def test_every_emitted_action_has_a_row():
    from core.packs import state
    from polyrob_x.twitter_tool import TwitterTool
    from polyrob_x.x_browser.tool import XBrowserTool
    seen = 0
    for tool_id, cls in (("twitter", TwitterTool), ("x_browser", XBrowserTool)):
        for attr in dir(cls):
            fn = getattr(cls, attr, None)
            if hasattr(fn, "_description"):          # a BaseTool.action
                name = attr if attr.startswith(tool_id + "_") else f"{tool_id}_{attr}"
                assert state.action_refusal(tool_id, name) is None, name
                seen += 1
    assert seen >= 29, seen   # 23 twitter + 6 x_browser verbs


def test_the_twitter_gate_is_the_credential_predicate(monkeypatch, tmp_path):
    import polyrob_x
    for key in ("TWITTER_OAUTH2_ACCESS_TOKEN", "TWITTER_OAUTH2_REFRESH_TOKEN",
                "TWITTER_API_KEY", "TWITTER_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(polyrob_x, "_x_oauth2_store_present", lambda: False)
    assert polyrob_x.twitter_gate() is False
    monkeypatch.setenv("TWITTER_API_KEY", "k")
    monkeypatch.setenv("TWITTER_ACCESS_TOKEN", "t")
    assert polyrob_x.twitter_gate() is True


_DISABLED = """
import asyncio, json, logging
import core.packs.loader as L
L.register_policies(); L.load_packs()
from cli.toolset import resolve_tool_list
from agents.task import tool_defaults as td
from agents.task import constants as c
from core.status_packs import packs_section
from core.surfaces import catalog
from cron import delivery
from tools.controller.tool_load_report import describe_missing_tool
class Job:
    id, user_id, session_id = "j1", "u1", ""
logs = []
class H(logging.Handler):
    def emit(self, r):
        logs.append(r.getMessage())
logging.getLogger("cron.delivery").addHandler(H())
out = asyncio.run(delivery.deliver_result_ex(object(), Job(), "report", target="twitter"))
print(json.dumps({
    "cli": resolve_tool_list(None, None)[0],
    "server": td.server_default_tools(),
    "social": td.resolve_toolset("social"),
    "grant": list(c.autonomous_mode_tools()),
    "packs": packs_section().lines,
    "explicit": describe_missing_tool("x_browser"),
    "surface": catalog.get("x") is None,
    "surface_ids": list(catalog.surface_ids()),
    "withheld": catalog.withheld_reason("x"),
    "targets": list(delivery.allowed_targets()),
    "cron": out,
    "cron_log": [m for m in logs if "channel 'twitter' unavailable" in m],
}))
"""


def test_disabled_pack_drops_out_and_is_named():
    env = {**os.environ, "POLYROB_PACKS_DISABLED": "x", "POLYROB_ENV_KEY_BACKFILL": "0",
           "AUTONOMY_MODE": "autonomous", "X_BROWSER_ENABLED": "true"}
    env.pop("POLYROB_PACKS", None)
    out = subprocess.run([sys.executable, "-c", _DISABLED], cwd=str(REPO), env=env,
                         capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stderr[-3000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    for key in ("cli", "server", "social", "grant"):
        assert "twitter" not in got[key] and "x_browser" not in got[key], (key, got[key])
    assert any(line.startswith("x ") and "disabled" in line for line in got["packs"]), got["packs"]
    assert "pack 'x'" in got["explicit"] and "disabled" in got["explicit"]
    assert got["surface"] is True and "x" not in got["surface_ids"]
    assert "pack 'x'" in got["withheld"] and "POLYROB_PACKS_DISABLED" in got["withheld"]
    assert "twitter" in got["targets"]          # a stored job's channel stays known
    assert got["cron"] == "unavailable"
    assert got["cron_log"] and "pack 'x'" in got["cron_log"][0], got["cron_log"]


def test_pack_skills_carry_their_rules_and_left_the_builtin_library():
    from agents.task.agent.skill_manager import SkillManager
    sm = SkillManager()
    sm._ensure_rules_loaded()
    assert {"x-engagement", "social-discovery"} <= sm._pack_rule_ids
    matched = sm.get_skills_for_session(tool_ids=["twitter", "x_browser"],
                                        task="reply to mentions on x", available_actions=[],
                                        user_id=None)
    assert "x-engagement" in [m.skill_id for m in matched]
    base = REPO / "data" / "prompts" / "skills"
    rules = json.loads((base / "rules.json").read_text())
    for sid in ("x-engagement", "social-discovery"):
        assert sid not in rules and not (base / sid).exists()
