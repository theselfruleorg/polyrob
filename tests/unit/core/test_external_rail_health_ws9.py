"""056 WS9 — external rails are HEALTH facts, not blocked goals discovered later.

The three standing owner asks (X browser session, the anysite user-search
degradation of 2026-09-18 19:48–22:30Z, the rejected Gmail password) each
resurfaced as Rob's blocked goals because no seat said them. Now: an absent X
login session while X_BROWSER_ENABLED=true is a WARN with the ceremony named;
the anysite tool records a `rail_probe` event per call and three consecutive
empty answers on the user-search endpoint are a WARN; the SMTP rejection is a
WARN (WS4).
"""
import json
import os
import time

import pytest


def _sec():
    from core.status_snapshot import Section
    return Section(name="identity")


def test_x_session_absent_is_a_warn_only_when_the_rail_is_enabled(monkeypatch, tmp_path):
    from core import status_snapshot as ss
    monkeypatch.setenv("X_BROWSER_ENABLED", "true")
    sec = _sec()
    ss._x_session_line(sec, str(tmp_path))
    assert any("X login session: none" in l for l in sec.lines)
    assert any(h.key == "x_session_absent" and "capture-session" in h.remedy for h in sec.health)
    monkeypatch.setenv("X_BROWSER_ENABLED", "false")
    sec2 = _sec()
    ss._x_session_line(sec2, str(tmp_path))
    assert not [h for h in sec2.health if h.key == "x_session_absent"]


def test_x_session_present_is_reported_ok(tmp_path, monkeypatch):
    from core import status_snapshot as ss
    monkeypatch.setenv("X_BROWSER_ENABLED", "true")
    (tmp_path / ".x_session.json").write_text(json.dumps({"rob|x": "b64"}))
    sec = _sec()
    ss._x_session_line(sec, str(tmp_path))
    assert any("X login session: stored" in l for l in sec.lines)
    assert not [h for h in sec.health if h.key == "x_session_absent"]


def test_oauth2_only_store_is_not_a_browser_session(tmp_path, monkeypatch):
    """Prod 2026-09-19 10:34Z: `.x_session.json` held only the API OAuth2 token
    (`rob|x_oauth2`); status said "stored" while x_browser refused with "no X
    session stored". The line must read the (user, "x") KEY the browser rail
    reads, not "file non-empty" — a confident-wrong status line is the class
    the status SSOT forbids."""
    from core import status_snapshot as ss
    monkeypatch.setenv("X_BROWSER_ENABLED", "true")
    (tmp_path / ".x_session.json").write_text(json.dumps({"rob|x_oauth2": "b64"}))
    sec = _sec()
    ss._x_session_line(sec, str(tmp_path))
    assert any("X login session: none" in l for l in sec.lines)
    assert any(h.key == "x_session_absent" for h in sec.health)
    assert sec.data["x_session"] is False


def test_three_empty_user_searches_are_a_warn():
    from core import status_snapshot as ss
    tele = ss.Section(name="telemetry")
    now = time.time()
    rows = [{"ts": now - i * 60, "kind": "rail_probe",
             "attrs": {"rail": "anysite:/api/twitter/search/users", "outcome": "empty"}}
            for i in range(3)]
    tele.data["rows"] = rows
    sec = ss.Section(name="loops")
    ss._external_rail_health(sec, tele)
    assert any(h.key == "anysite_search_empty" for h in sec.health)
    # one OK answer in between breaks the streak
    rows[1]["attrs"]["outcome"] = "ok"
    sec2 = ss.Section(name="loops")
    ss._external_rail_health(sec2, tele)
    assert not [h for h in sec2.health if h.key == "anysite_search_empty"]


@pytest.mark.asyncio
async def test_anysite_api_records_a_rail_probe_event(monkeypatch, tmp_path):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)
    from tools.anysite import tool as at
    from types import SimpleNamespace
    monkeypatch.setattr(at.AnysiteTool, "_prepare", lambda self: True)
    monkeypatch.setattr(at, "build_api_argv", lambda e, p, f: ["x"])

    async def _run(argv):
        return SimpleNamespace(timed_out=False, exit_code=0, stdout="[]", stderr="")
    monkeypatch.setattr(at, "run_anysite", _run)
    t = object.__new__(at.AnysiteTool)
    res = await t.anysite_api(at.AnysiteApiParams(endpoint="/api/twitter/search/users",
                                                  params={"query": "x"}))
    rows = log.query(kind="rail_probe")
    assert rows and rows[0]["attrs"]["rail"] == "anysite:/api/twitter/search/users"
    assert rows[0]["attrs"]["outcome"] == "empty"
