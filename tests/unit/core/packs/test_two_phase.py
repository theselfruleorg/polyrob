"""The two-phase design, proved in fresh interpreters (067 P2).

Phase 1 runs where the CLI's ``main()`` and ``api/app.py`` run it — after
``import core`` (which builds no policy view: the views are lazy, built on first
read, ``core/lazy_views.py``) and before any view is read. A pack row registered there is in every view built later.
A row registered after its view was built is refused, naming the view.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
ECHO = REPO / "tests" / "fixtures" / "packs" / "echo"

_PRELUDE = f"""
import sys, json
sys.path.insert(0, {str(ECHO)!r})
from importlib.metadata import EntryPoint
import core.packs.loader as L
from core.packs import state
L._entry_points = lambda: [EntryPoint("echo", "polyrob_echo:pack", "polyrob.packs")]
L._first_party_refusal = lambda rec, ep: None  # synthetic reviewed fixture
"""


def _run(body: str) -> dict:
    out = subprocess.run([sys.executable, "-c", _PRELUDE + body], cwd=str(REPO),
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_phase_one_at_entry_puts_pack_rows_in_every_later_view():
    got = _run("""
L.register_policies()
rec = state.record("echo")
import core.effects, core.config_policy.spend_lane, core.surfaces.room_policy
import core.security.untrusted_wrap as uw
import agents.task.agent.skill_manager as sm
import core.bootstrap as bs
print(json.dumps({
    "status": rec.status, "reason": rec.reason,
    "non_write": sorted(core.effects.NON_WRITE_ACTIONS.get("echo", ())),
    "valid_tool": "echo" in sm.VALID_TOOL_IDS,
    "cli_incompatible": "echo" in bs._CLI_INCOMPATIBLE,
    "untrusted": "echo" in uw.UNTRUSTED_TOOL_NAMESPACES,
}))
""")
    assert got["status"] == "installed", got["reason"]
    assert got["non_write"] == ["echo_say"]
    assert got["valid_tool"] is True
    assert got["cli_incompatible"] is False and got["untrusted"] is False


def test_views_built_before_phase_one_refuse_the_pack_by_name():
    got = _run("""
import core.effects
core.effects.NON_WRITE_ACTIONS   # the first read builds the view (lazy since 067 P4)
L.register_policies()
rec = state.record("echo")
from core.tool_capabilities import is_classified
print(json.dumps({"status": rec.status, "reason": rec.reason,
                  "classified": is_classified("echo")}))
""")
    assert got["status"] == "refused"
    assert "core.effects" in got["reason"]
    assert got["classified"] is False


def test_the_cli_entry_runs_phase_one_before_any_subcommand_import():
    """``polyrob version`` through ``main()``: the pack is discovered and its
    rows are in the views the process builds afterwards."""
    got = _run("""
import cli.polyrob as P
sys.argv = ["polyrob", "version"]
try:
    P.main()
except SystemExit:
    pass
rec = state.record("echo")
import core.effects
print(json.dumps({"status": rec.status, "reason": rec.reason,
                  "non_write": sorted(core.effects.NON_WRITE_ACTIONS.get("echo", ()))}))
""")
    assert got["status"] == "installed", got["reason"]
    assert got["non_write"] == ["echo_say"]


def test_the_api_app_module_runs_phase_one_at_import():
    got = _run("""
import api.app
rec = state.record("echo")
import core.effects
print(json.dumps({"status": rec.status, "reason": rec.reason,
                  "non_write": sorted(core.effects.NON_WRITE_ACTIONS.get("echo", ()))}))
""")
    assert got["status"] == "installed", got["reason"]
    assert got["non_write"] == ["echo_say"]


def test_console_status_before_chat_does_not_make_packs_too_late():
    got = _run("""
import tempfile
import webview.server
from core.status_snapshot import build_status_snapshot
with tempfile.TemporaryDirectory() as home:
    snap = build_status_snapshot("owner", data_dir=home, include_money=False)
L.load_packs()
rec = state.record("echo")
print(json.dumps({"status": rec.status, "reason": rec.reason}))
""")
    assert got["status"] == "loaded", got["reason"]
