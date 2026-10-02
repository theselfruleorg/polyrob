"""067 (one install): a first-party pack ships inside polyrob; its SDKs do not.
A tool whose ``requires`` are absent is withheld (never registered, never
offered, its actions refused with the remedy) and every surface names the
pack's extra — ``polyrob pack list``, the status section, ``polyrob doctor``'s
line and the environment facts all read ``core.packs.sdk``."""
from pathlib import Path

import pytest

from core.packs import state
from tests.unit.core.packs.conftest import write_pack
from tests.unit.core.packs.test_loader import _init, _install, _toml

REPO = Path(__file__).resolve().parents[4]

SDKLESS = _toml("sdkp").replace('tier = "first-party"\n', 'tier = "first-party"\nextra = "twitter"\n') \
    .replace("[tools.sdkpt]\n", '[tools.sdkpt]\nrequires = ["polyrob_no_such_sdk_module"]\n')


@pytest.fixture
def sdkless(scratch, tmp_path, monkeypatch):
    registered = tmp_path / "registered"
    write_pack(tmp_path, monkeypatch, "sdkp", SDKLESS, _init("sdkp").replace(
        "registrar=lambda: True",
        f"registrar=lambda: __import__('pathlib').Path({str(registered)!r}).touch()"))
    _install(scratch, monkeypatch, ["sdkp"])
    monkeypatch.setenv("LAZY_DEPS_MODE", "off")
    scratch.load_packs()
    return registered


def test_a_tool_without_its_sdk_is_withheld_and_named(sdkless):
    rec = state.record("sdkp")
    assert rec.status == state.LOADED, rec.reason
    assert not sdkless.exists(), "the withheld tool's registrar must not run"
    from core.tool_gates import gate_on
    assert not gate_on("sdkpt")
    assert state.tool_withheld("sdkpt")
    why = state.action_refusal("sdkpt", "sdkpt_read")
    assert why and "pip install 'polyrob[twitter]'" in why and "withheld" in why
    line = rec.line()
    assert "loaded" in line and "needs `pip install 'polyrob[twitter]'`" in line, line
    assert "sdkp: needs `pip install 'polyrob[twitter]'`" in state.summary_line()


def test_the_status_section_carries_the_need(sdkless):
    from core.status_packs import packs_section
    sec = packs_section()
    row = next(r for r in sec.data["packs"] if r["id"] == "sdkp")
    assert row["needs"] == [{"tool": "sdkpt", "missing": ["polyrob_no_such_sdk_module"],
                             "remedy": "pip install 'polyrob[twitter]'", "withheld": True}]
    assert any("polyrob[twitter]" in line for line in sec.lines)


def test_the_environment_facts_read_the_same_source(sdkless):
    from core.install_facts import missing_extras
    rows = [r for r in missing_extras() if r[0] == "twitter"]
    assert rows == [("twitter", "the sdkp pack's sdkpt tool", "pip install 'polyrob[twitter]'")]


def test_with_its_sdk_the_tool_registers(scratch, tmp_path, monkeypatch):
    toml = SDKLESS.replace("polyrob_no_such_sdk_module", "json")
    registered = tmp_path / "registered"
    write_pack(tmp_path, monkeypatch, "sdkp", toml, _init("sdkp").replace(
        "registrar=lambda: True",
        f"registrar=lambda: __import__('pathlib').Path({str(registered)!r}).touch()"))
    _install(scratch, monkeypatch, ["sdkp"])
    scratch.load_packs()
    rec = state.record("sdkp")
    assert rec.status == state.LOADED and rec.needs == []
    assert registered.exists() and not state.tool_withheld("sdkpt")
    assert "needs" not in rec.line()


def test_a_lazy_extra_keeps_the_tool_offered(scratch, tmp_path, monkeypatch):
    """anysite has a trusted lazy closure: where lazy installs are on the tool
    stays offered (first use installs it) and the state says so."""
    toml = SDKLESS.replace('extra = "twitter"', 'extra = "anysite"')
    write_pack(tmp_path, monkeypatch, "sdkp", toml, _init("sdkp"))
    _install(scratch, monkeypatch, ["sdkp"])
    monkeypatch.setenv("LAZY_DEPS_MODE", "trusted")
    scratch.load_packs()
    rec = state.record("sdkp")
    assert rec.status == state.LOADED and not state.tool_withheld("sdkpt")
    assert "installs on first use" in rec.line(), rec.line()


def test_every_first_party_pack_declares_a_real_extra_and_its_sdk_probes():
    """The three in-wheel packs name a core extra and probe the SDK modules that
    extra installs (one source: pyproject extras <- pack.toml)."""
    import tomllib
    from core.optional_extras import EXTRA_FOR_MODULE
    from core.packs.manifest import read
    extras = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    want = {"polyrob_x": "twitter", "polyrob_discovery": "anysite", "polyrob_markets": "crypto"}
    for module, extra in want.items():
        m = read(module)
        assert m.extra == extra and extra in extras
        probes = [mod for t in m.tools for mod in t.requires]
        assert probes, module
        for mod in probes:
            assert EXTRA_FOR_MODULE.get(mod.split(".")[0], extra) == extra, (module, mod)


def test_before_phase_two_the_state_line_still_names_the_need(scratch, tmp_path, monkeypatch):
    """`polyrob doctor` renders the status snapshot before phase 2: an INSTALLED
    record probes its manifest the same way, never a bare "installed"."""
    write_pack(tmp_path, monkeypatch, "sdkp", SDKLESS, _init("sdkp"))
    _install(scratch, monkeypatch, ["sdkp"])
    monkeypatch.setenv("LAZY_DEPS_MODE", "off")
    scratch.register_policies()
    rec = state.record("sdkp")
    assert rec.status == state.INSTALLED
    assert "installed — needs `pip install 'polyrob[twitter]'`" in rec.line(), rec.line()
    from core.status_packs import packs_section
    row = next(r for r in packs_section().data["packs"] if r["id"] == "sdkp")
    assert row["needs"] and row["needs"][0]["withheld"]
