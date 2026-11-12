"""058 T4.2 — the ``tools`` status section."""
import json
import os
import time

import pytest

from core.status_snapshot import SECTION_ORDER, STATE_UNAVAILABLE, build_status_snapshot
from core.status_tools import tools_section


def _log(data_dir):
    from core.event_log import TelemetryEventLog
    return TelemetryEventLog(os.path.join(data_dir, "telemetry_events.db"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", d)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", os.path.join(d, "telemetry_events.db"))
    return d


def test_absent_log_is_never_created_by_a_read(home):
    sec = tools_section("u1", home)
    assert sec.state == "ok"
    assert "unrecorded" in sec.lines[0]
    assert not os.path.exists(os.path.join(home, "telemetry_events.db"))


def test_log_with_no_tool_rows_says_unrecorded_not_zero(home):
    _log(home).record("usage", user_id="u1", source="llm", attrs={"x": 1})
    sec = tools_section("u1", home)
    assert "unrecorded" in sec.lines[0]
    assert "0" not in sec.lines[0].split("(")[0]


def test_counts_last_and_never_invoked_wording(home):
    log = _log(home)
    now = time.time()
    for i in range(3):
        log.record("tool_invoked", user_id="u1", source="tool", ts=now - 60 * i,
                   attrs={"tool": "filesystem", "action": "read_file", "ok": i != 2, "ms": 5})
    log.record("tool_invoked", user_id="u1", source="tool", ts=now - 5,
               attrs={"tool": "browser", "action": "browser_click", "ok": True, "ms": 900})
    sec = tools_section("u1", home, now=now)
    text = "\n".join(sec.lines)
    assert sec.data["tools"]["filesystem"]["count"] == 3
    assert sec.data["tools"]["filesystem"]["errors"] == 1
    assert "2 of" in sec.lines[0] and "recording since" in sec.lines[0]
    assert "filesystem: 3 ×, 1 err" in text
    # a tool with no row is "never invoked (recording since …)", NEVER "0"
    assert "never invoked (recording since" in text
    assert "coding" in text.split("never invoked")[1]
    assert "coding: 0" not in text


def test_other_tenant_rows_are_not_counted(home):
    _log(home).record("tool_invoked", user_id="someone-else", source="tool",
                      attrs={"tool": "browser", "action": "x", "ok": True, "ms": 1})
    sec = tools_section("u1", home)
    assert "browser" not in sec.data["tools"]


def test_unparseable_row_is_counted_and_named(home):
    log = _log(home)
    log.record("tool_invoked", user_id="u1", source="tool", attrs={"tool": "task", "ok": True})
    from core.sqlite_util import execute_retry
    execute_retry(os.path.join(home, "telemetry_events.db"),
                  "UPDATE telemetry_events SET attrs = '{not json' WHERE kind = 'tool_invoked'", (), fetch=None)
    sec = tools_section("u1", home)
    assert sec.data["unparseable"] == 1
    assert "unparseable" in "\n".join(sec.lines)


def test_corrupt_log_renders_unavailable_with_reason(home):
    with open(os.path.join(home, "telemetry_events.db"), "wb") as fh:
        fh.write(b"garbage" * 100)
    snap = build_status_snapshot("u1", data_dir=home, include_money=False)
    sec = snap.section("tools")
    assert sec.state == STATE_UNAVAILABLE and sec.reason


def test_section_is_in_order_and_titled(home):
    assert "tools" in SECTION_ORDER
    from core.status_render import _SECTION_TITLES, render_status_lines
    assert _SECTION_TITLES["tools"] == "Tools"
    snap = build_status_snapshot("u1", data_dir=home, include_money=False)
    assert tuple(snap.sections) == SECTION_ORDER
    assert any("Tools:" in l for l in render_status_lines(snap))


def test_the_headline_and_the_rows_count_the_same_set(home):
    """116 W5.2: '17 of 30 tools' sat above rows that also listed `core` and
    `unknown`; they are not tools and get their own lines, `core` by action."""
    log = _log(home)
    now = time.time()
    log.record("tool_invoked", user_id="u1", ts=now, attrs={"tool": "filesystem", "action": "read_file", "ok": True})
    for action in ("done", "done", "send_message"):
        log.record("tool_invoked", user_id="u1", ts=now, attrs={"tool": "core", "action": action, "ok": True})
    log.record("tool_invoked", user_id="u1", ts=now, attrs={"action": "x", "ok": True})
    sec = tools_section("u1", home, now=now)
    assert sec.lines[0].startswith("1 of ")
    rows = [ln for ln in sec.lines if " × " in ln or ln.endswith("×")]
    assert [ln.split(":")[0] for ln in rows if "last" in ln] == ["filesystem"]
    text = "\n".join(sec.lines)
    assert "core actions (not a tool): 3 × — done 2, send_message 1" in text
    assert "not in the tool table: 1 call(s)" in text
