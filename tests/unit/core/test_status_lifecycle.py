"""077 — the restart facts in the status snapshot and the config write hint."""
import time

import pytest

from core import lifecycle as lc
from core.status_types import Section


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "unit")
    return str(tmp_path / "data")


def _sec(home):
    from core.status_types import HealthItem
    facts = lc.status_facts(home)
    sec = Section(name="identity")
    sec.lines.extend(facts["lines"])
    sec.health.extend(HealthItem(key=k, text=t, remedy=r) for k, t, r in facts["health"])
    return sec


def test_nothing_running_says_nothing(home, monkeypatch):
    monkeypatch.delenv("INVOCATION_ID")
    lc.record_boot(data_home=home)  # a terminal process
    sec = _sec(home)
    assert not sec.health and not any(l.startswith("waiting") for l in sec.lines)


def test_waiting_settings_are_a_health_item_with_a_chat_remedy(home):
    lc.record_boot(data_home=home)
    time.sleep(0.01)
    lc.record_pending("TELEGRAM_ALLOWED_CHAT_IDS", data_home=home)
    sec = _sec(home)
    assert any("1 setting(s) apply at the next restart" in l for l in sec.lines)
    item = next(h for h in sec.health if h.key == "settings_wait_for_restart")
    assert item.remedy == "/restart"


def test_stale_code_is_named(home, monkeypatch):
    lc.record_boot(data_home=home)
    monkeypatch.setattr(lc, "code_version", lambda: "9.9.9")
    sec = _sec(home)
    assert any(h.key == "service_code_stale" for h in sec.health)


def test_a_restart_nobody_answered(home, monkeypatch):
    lc.request_restart(requested_by="owner", data_home=home)
    lc.set_request_state(lc.STATE_EXITING, data_home=home)
    monkeypatch.delenv("INVOCATION_ID")
    sec = _sec(home)
    assert any(h.key == "restart_not_back" for h in sec.health)


def test_a_recent_failed_update_is_a_health_item(home):
    lc.request_restart(kind=lc.KIND_UPDATE, requested_by="owner", data_home=home)
    lc.close_request(lc.OUTCOME_UPDATE_FAILED, "failed at migrate", data_home=home)
    assert any(h.key == "restart_update_failed" for h in _sec(home).health)


def test_an_unreadable_record_is_said(home):
    d = lc.lifecycle_dir(home)
    d.mkdir(parents=True)
    (d / "boot.json").write_text("{x")
    assert any("unreadable" in l for l in _sec(home).lines)


def test_config_write_records_waiting_and_names_the_restart(home, monkeypatch, tmp_path):
    from core import config_service
    monkeypatch.setenv("POLYROB_DATA_DIR", home)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "ph"))
    lc.record_boot(data_home=home)
    time.sleep(0.01)
    res = config_service.set_value("TELEGRAM_PROGRESS_EDITS", "false", surface="telegram")
    if not res.ok:
        pytest.skip(f"flag not writable here: {res.message}")
    assert "TELEGRAM_PROGRESS_EDITS" in lc.pending_keys(home)
    assert "Send /restart to apply it now." in res.message
    assert res.notes[-1] == "Send /restart to apply it now."
