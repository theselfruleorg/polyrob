"""pack.toml parsing and validation (067 P2) — no pack code is imported."""
import sys
from pathlib import Path

import pytest

from core.packs import manifest as m

GOOD = {
    "id": "demo", "version": "0.1.0", "pack_api": 1, "tier": "first-party",
    "capabilities": ["tools", "egress", "high_impact"],
    "cli": {"commands": ["demo"]},
    "tools": {"demo": {"capabilities": ["high_impact", "writes_network"],
                       "untrusted_output": True, "cli": "incompatible",
                       "gate": {"flag": "DEMO_ENABLED"},
                       "verbs": {"demo_fetch": {"effect": "network"},
                                 "demo_read": {"effect": "none", "room_denied": True}}}},
}


def _parse(**over):
    data = {**GOOD, **over}
    return m.parse(data, Path("pack.toml"))


def test_a_valid_manifest_parses_into_rows():
    man = _parse()
    (tool,) = man.tools
    assert tool.id == "demo" and "writes_network" in tool.row and tool.row.untrusted_output
    assert tool.row.cli == "incompatible" and tool.row.gate.flag == "DEMO_ENABLED"
    assert {v.name for v in tool.verbs} == {"demo_fetch", "demo_read"}
    assert all(v.tool == "demo" for v in tool.verbs)
    assert man.derived_capabilities() == {"tools", "egress", "high_impact"}
    assert man.cli_commands == ("demo",)


def test_static_cli_mode_and_permissions_parse():
    man = _parse(tools={"demo": {"cli": "static", "permissions": ["network.read"],
                                 "verbs": {"demo_x": {}}}}, capabilities=["tools"])
    (tool,) = man.tools
    assert tool.row.cli == "static" and tool.permissions == ("network.read",)


@pytest.mark.parametrize("over, match", [
    ({"pack_api": 2}, "pack_api 2 is not supported"),
    ({"pack_api": "1"}, "pack_api must be an integer"),
    ({"surprise": 1}, "unknown key"),
    ({"id": "Demo"}, "must match"),
    ({"tier": "vendor"}, "tier"),
    ({"capabilities": ["teleport"]}, "not supported by pack_api 1"),
    ({"console": {"public_paths": ["/cb"]}}, "needs the console_routes capability"),
    ({"console": {"public_paths": ["/cb"]}, "capabilities": ["console_routes"],
      "tier": "third-party"}, "third-party pack may not declare public console paths"),
    ({"console": {"public_paths": ["/cb/"]}, "capabilities": ["console_routes"],
      "tier": "first-party"}, "must be exact paths"),
    ({"console": {"public_paths": ["/cb?x=1"]}, "capabilities": ["console_routes"],
      "tier": "first-party"}, "must be exact paths"),
    ({"console": {"public_paths": ["/a/*"]}, "capabilities": ["console_routes"],
      "tier": "first-party"}, "must be exact paths"),
    ({"console": {"routes": []}}, "holds only public_paths"),
    ({"cli": {"commands": ["Bad Name"]}}, "cli command name"),
    ({"tools": {"demo": {"verbs": {"other_x": {}}}}}, "must be named 'demo_<verb>'"),
    ({"tools": {"demo": {"capabilities": []}}}, "classifies its actions"),
    ({"tools": {"demo": {"cli": "optional", "verbs": {"demo_x": {}}}}}, "cli must be one of"),
    ({"tools": {"demo": {"permissions": "network.read", "verbs": {"demo_x": {}}}}},
     "permissions must be a list"),
    ({"tools": {"demo": {"capabilities": ["teleport"], "verbs": {"demo_x": {}}}}},
     "unknown capability token"),
    ({"tools": {"demo": {"verbs": {"demo_x": {"effect": "magic"}}}}}, "unknown effect"),
    ({"tools": {"demo": {"verbs": {"demo_x": {"tool": "other"}}}}}, "table of policy fields"),
])
def test_malformed_manifests_are_named(over, match):
    with pytest.raises(m.ManifestError, match=match):
        _parse(**over)


def test_a_first_party_pack_declares_exact_public_console_paths():
    man = _parse(capabilities=["console_routes"], tier="first-party",
                 console={"public_paths": ["/oauth/callback"]})
    assert man.console_public_paths == ("/oauth/callback",)
    assert "console_routes" not in man.derived_capabilities()   # checked in phase 2


@pytest.mark.parametrize("field", ["simulatable", "risk_reducing", "room_denied",
                                  "correspondent_blocked", "untrusted_output"])
@pytest.mark.parametrize("value", ["true", "false", 1, 0])
def test_security_booleans_reject_strings_and_numbers(field, value):
    # A quoted TOML boolean otherwise misses views queried with field=True.
    verb = {"lane": "defi"}
    tool = {"verbs": {"demo_write": verb}}
    (tool if field == "untrusted_output" else verb)[field] = value
    with pytest.raises(m.ManifestError, match=f"{field} must be a boolean"):
        _parse(tools={"demo": tool}, capabilities=["tools"])


def test_read_locates_the_file_without_importing_the_package(tmp_path, monkeypatch):
    pkg = tmp_path / "polyrob_probe"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("raise RuntimeError('imported!')\n")
    (pkg / "pack.toml").write_text('id = "probe"\npack_api = 1\ntier = "third-party"\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    man = m.read("polyrob_probe.sub")
    assert man.id == "probe" and man.tier == "third-party" and man.tools == ()
    assert "polyrob_probe" not in sys.modules


def test_a_missing_manifest_is_named(tmp_path, monkeypatch):
    (tmp_path / "polyrob_bare").mkdir()
    (tmp_path / "polyrob_bare" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(m.ManifestError, match="ships no pack.toml"):
        m.read("polyrob_bare")
    with pytest.raises(m.ManifestError, match="not installed"):
        m.read("polyrob_absent_xyz")
