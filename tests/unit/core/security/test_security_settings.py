"""Owner decision D1 (2026-10-10): if a check can refuse natural work, it is an
owner-set security SETTING, never a hard-coded refusal. ONE typed table
(``core.security.settings``), ONE owner-only store (``SECURITY_SETTINGS``,
Access ``file``), defaults that do not refuse natural work, and a short
enumerated floor that is not configurable."""
import pytest

from core.security import settings as ss


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(ss.ENV, raising=False)


def test_every_row_is_typed_and_names_its_readers():
    names = [r.name for r in ss.SETTINGS]
    assert len(names) == len(set(names))
    for row in ss.SETTINGS:
        assert row.default in row.choices, row
        assert row.sites and row.description, row


def test_defaults_do_not_refuse_natural_work():
    assert ss.value("host_exec_under_custody") == "allow"
    assert ss.value("packs_under_custody") == "allow"
    assert ss.value("unattended_dangerous_shell") == "sandbox"


def test_the_owner_value_wins(monkeypatch):
    monkeypatch.setenv(ss.ENV, "host_exec_under_custody=refuse, READ_TAINT = off")
    assert ss.value("host_exec_under_custody") == "refuse"
    assert ss.value("read_taint") == "off"
    assert ss.value("packs_under_custody") == "allow"
    assert ss.invalid_entries() == []


def test_invalid_entries_keep_the_default_and_are_reported(monkeypatch):
    monkeypatch.setenv(ss.ENV, "host_exec_under_custody=refuze,nosuch=1,packs_under_custody")
    assert ss.value("host_exec_under_custody") == "allow"
    assert sorted(ss.invalid_entries()) == sorted(
        ["host_exec_under_custody=refuze", "nosuch=1", "packs_under_custody"])


def test_an_unknown_row_name_is_a_programming_error():
    with pytest.raises(KeyError):
        ss.value("no_such_row")


def test_the_store_is_owner_only():
    """Access ``file``: only the operator env file or the local CLI writes it —
    never the console, a chat pref or an agent tool."""
    from core.config_service import is_console_unwritable
    from core.flags_catalog import ACCESS
    assert ACCESS.get(ss.ENV) == "file"
    assert is_console_unwritable(ss.ENV)


def test_no_pref_key_can_set_a_security_row():
    from core.prefs import PREF_SCHEMA
    assert not [k for k in PREF_SCHEMA if k.startswith("security")]


def test_the_floor_is_small_and_enumerated():
    assert 1 <= len(ss.FLOOR) <= 8
    for row in ss.FLOOR:
        assert row.name and row.description and row.sites
    assert not {r.name for r in ss.FLOOR} & {r.name for r in ss.SETTINGS}


def test_the_status_line_names_every_row_and_its_source(monkeypatch):
    monkeypatch.setenv(ss.ENV, "read_taint=off,bogus=1")
    data = ss.effective()
    by = {r["name"]: r for r in data}
    assert set(by) == {r.name for r in ss.SETTINGS}
    assert by["read_taint"] == {**by["read_taint"], "value": "off", "source": "owner"}
    assert by["host_exec_under_custody"]["source"] == "default"
    line = ss.status_line()
    assert "read_taint=off (owner)" in line and "host_exec_under_custody=allow" in line


def test_the_custody_status_slot_shows_the_effective_settings(monkeypatch):
    """The owner reads them where status already lives (/status, polyrob doctor)."""
    import core.status_custody as sc
    monkeypatch.setenv(ss.ENV, "host_exec_under_custody=refuse,bogus=1")
    monkeypatch.setattr(sc, "custody_section",
                        lambda: sc.Section(name="custody", lines=["x"], data={}))
    sec = sc.custody_with_settings()
    assert any("host_exec_under_custody=refuse (owner)" in ln for ln in sec.lines)
    assert sec.data["security_settings"]
    assert "security_settings_invalid" in {h.key for h in sec.health}


def test_settings_module_is_pure():
    import subprocess
    import sys
    from pathlib import Path
    code = ("import sys, core.security.settings; leaked = sorted(m for m in sys.modules "
            "if m.split('.')[0] in ('agents', 'tools', 'modules')); "
            "raise SystemExit(1 if leaked else 0)")
    root = Path(__file__).resolve().parents[4]
    assert subprocess.run([sys.executable, "-c", code], cwd=root, timeout=60).returncode == 0


def test_the_financial_claim_gate_is_an_owner_row(monkeypatch):
    """W4 owner note (2026-10-10): the claim gate (all rails, the X browser rail
    included) refuses outbound text, so it is a settings row; default on."""
    from tools.controller.financial_claim_gate import gate_enabled
    monkeypatch.delenv("FINANCIAL_CLAIM_GATE_ENABLED", raising=False)
    assert ss.value("financial_claim_gate") == "on" and gate_enabled()
    monkeypatch.setenv(ss.ENV, "financial_claim_gate=off")
    assert not gate_enabled()
    monkeypatch.delenv(ss.ENV)
    monkeypatch.setenv("FINANCIAL_CLAIM_GATE_ENABLED", "false")   # the older flag still works
    assert not gate_enabled()
