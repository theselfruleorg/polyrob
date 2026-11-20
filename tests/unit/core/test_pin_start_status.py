"""A pinned provider whose client did not start is an owner-visible fact.

An experiment box, 2026-11-20: DEFAULT_PROVIDER=zai-coding, the anthropic SDK did not
install, LLMManager fell through to OpenRouter with a log line only, and the
status said ``live provider: zai-coding``. The manager records the pin's start
(``core.runtime_config.record_pin_outcome``); the providers section reads the
latest record and names the provider the calls really go to.
"""
import json
import sqlite3
import time

import pytest

import core.runtime_config as rc
from core.status_session import _pin_did_not_start, _providers_section
from core.status_types import Section


@pytest.fixture
def emitted(monkeypatch):
    seen = []
    import core.event_log as el
    monkeypatch.setattr(el, "emit", lambda kind, **kw: seen.append((kind, kw["attrs"])))
    return seen


def test_no_pin_records_nothing(monkeypatch, emitted):
    monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    rc.record_pin_outcome(["openrouter_client"], "openrouter_client", [])
    assert emitted == []


def test_a_pin_that_did_not_start_records_the_error_and_the_provider_serving(monkeypatch, emitted):
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")
    rc.record_pin_outcome(["openrouter_client"], "openrouter_client",
                          [("zai-coding", False, "provider.anthropic: pip failed"),
                           ("openrouter", True, None)])
    assert emitted == [(rc.PIN_UNAVAILABLE, {"pin": "zai-coding", "primary": "openrouter",
                                             "error": "provider.anthropic: pip failed"})]


def test_a_pin_that_started_records_ready(monkeypatch, emitted):
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")
    rc.record_pin_outcome(["zai-coding_client", "openrouter_client"], "zai-coding_client",
                          [("zai-coding", True, None)])
    assert [k for k, _ in emitted] == [rc.PIN_READY]


def _tele(tmp_path, *events):
    tmp_path.mkdir(exist_ok=True)
    path = tmp_path / "telemetry.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE telemetry_events (ts REAL, kind TEXT, user_id TEXT, "
                "session_id TEXT, source TEXT, attrs TEXT)")
    for ts, kind, attrs in events:
        con.execute("INSERT INTO telemetry_events VALUES (?,?,?,?,?,?)",
                    (ts, kind, "", "", "llm_manager", json.dumps(attrs)))
    con.commit()
    con.close()
    return Section(name="telemetry", data={"path": str(path), "rows": []})


DOWN = {"pin": "zai-coding", "primary": "openrouter", "error": "provider.anthropic: pip failed"}


def test_the_latest_record_wins(tmp_path):
    now = time.time()
    tele = _tele(tmp_path, (now - 9 * 86400, rc.PIN_UNAVAILABLE, DOWN))   # a week+ old: still read
    assert _pin_did_not_start(tele, "zai-coding")["primary"] == "openrouter"
    tele = _tele(tmp_path / "b", (now - 60, rc.PIN_UNAVAILABLE, DOWN), (now, rc.PIN_READY, {"pin": "zai-coding"}))
    assert _pin_did_not_start(tele, "zai-coding") is None


def test_a_changed_pin_ignores_the_old_record(tmp_path):
    tele = _tele(tmp_path, (time.time(), rc.PIN_UNAVAILABLE, DOWN))
    assert _pin_did_not_start(tele, "deepseek") is None
    assert _pin_did_not_start(tele, None) is None


def test_the_providers_section_names_the_provider_serving(tmp_path, monkeypatch):
    import core.credit_sentinel as cs
    monkeypatch.setattr(cs, "credit_sentinel_status", lambda: {})
    monkeypatch.setattr(rc, "usable_providers_with_credentials", lambda: ["zai-coding", "openrouter"])
    monkeypatch.setattr(rc, "operator_provider_pin", lambda env=None: "zai-coding")
    monkeypatch.setattr(rc, "resolve_live_provider", lambda pin: "zai-coding")
    tele = _tele(tmp_path, (time.time(), rc.PIN_UNAVAILABLE, DOWN))
    sec = _providers_section(tele, time.time())
    item = next(h for h in sec.health if h.key == "pin_unavailable")
    assert "zai-coding did not start" in item.text and "calls go to openrouter" in item.text
    assert "pip failed" in item.text
    assert sec.lines[0].startswith("live provider: openrouter (pin zai-coding — dead, rerouted)")
