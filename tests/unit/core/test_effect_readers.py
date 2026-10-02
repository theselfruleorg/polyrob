"""033 §3.6: the readers read the ONE effect record, not closed kind lists.

- the X repeat-post cooldown: effect ``social`` + a tool/action discriminator
- ``pause_violation``: every AUTONOMOUS outward act after the pause, by class
- the digest / insights / activity feed / Work Log classes
"""
import logging
import time
from unittest.mock import MagicMock

import pytest

import core.event_log as el


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "telemetry_events.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    el._INSTANCES.clear()
    yield tmp_path
    el._INSTANCES.clear()


def _write(effect, *, tool="twitter", action="twitter_post", autonomous=True,
           outcome="ok", user_id="rob", ts=None):
    from core.effects import record_external_write
    record_external_write(effect=effect, tool=tool, action=action,
                          autonomous=autonomous, outcome=outcome, user_id=user_id)
    if ts is not None:  # backdate the newest row
        from core.sqlite_util import execute_retry
        execute_retry(el.get_event_log().db_path,
                      "UPDATE telemetry_events SET ts=? WHERE id=(SELECT MAX(id) FROM telemetry_events)",
                      (ts,))


# --- the X cooldown -------------------------------------------------------------

def _tool():
    TwitterTool = pytest.importorskip("polyrob_x.twitter_tool").TwitterTool  # X pack (067 P3b)
    t = object.__new__(TwitterTool)
    t.logger = logging.getLogger("t")
    t._container = MagicMock()
    return t


def _ctx(user_id="rob"):
    from tools.controller.execution_context import ActionExecutionContext
    return ActionExecutionContext(session_id="s", user_id=user_id)  # role leaf


@pytest.mark.asyncio
async def test_cooldown_reads_the_effect_record(home):
    _write("social")
    block = await _tool()._social_cooldown_block("twitter_post", _ctx())
    assert block and "cooldown" in block


@pytest.mark.asyncio
async def test_another_producers_social_write_never_cross_blocks(home):
    """The kind-only query let any social producer block twitter_post."""
    _write("social", tool="x_browser", action="x_browser_x_post")
    _write("social", tool="twitter", action="twitter_reply")
    _write("social", tool="twitter", action="twitter_post", outcome="denied")
    assert await _tool()._social_cooldown_block("twitter_post", _ctx()) is None


@pytest.mark.asyncio
async def test_a_legacy_social_write_row_still_counts(home):
    from core.event_kinds import SOCIAL_WRITE
    el.get_event_log().record(SOCIAL_WRITE, user_id="rob", source="twitter_tool")
    assert await _tool()._social_cooldown_block("twitter_thread", _ctx())


# --- pause_violation ---------------------------------------------------------------

def _violation(home):
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("rob", data_dir=str(home), include_money=False)
    v = [h for h in snap.health if h.key == "pause_violation"]
    return v[0].text if v else ""


def test_an_autonomous_write_after_the_pause_is_a_violation(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("social",), via="test")
    _write("social", tool="x_browser", action="x_browser_x_post")   # a NEW writer
    assert "social write ×1" in _violation(home)


def test_what_the_pause_does_not_cover_is_not_a_violation(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("social",), via="test")
    _write("money", tool="defi_trade", action="defi_trade_swap")    # not under social
    _write("social", outcome="denied")                              # the pause working
    _write("social", autonomous=False)                              # the owner's own post
    _write("network", tool="mcp", action="srv_x")                   # observe-only
    assert _violation(home) == ""


def test_a_write_before_the_pause_is_not_a_violation(home):
    from core import autonomy_control as ac
    _write("social", ts=time.time() - 3600)
    ac.pause(str(home), scopes=("all",), via="test")
    assert _violation(home) == ""


# --- digest / insights / feed -------------------------------------------------------

def test_outward_counts_and_line(home):
    from core.effects import outward_counts, outward_line
    assert outward_counts("rob", 0) == {}               # no store: nothing recorded
    _write("social")
    _write("social", user_id="")                         # untenanted single-owner row
    _write("comms", tool="email", action="email_send")
    _write("social", user_id="someone-else")
    _write("money", outcome="error")
    counts = outward_counts("rob", 0)
    assert counts == {"social": 2, "comms": 1}
    assert outward_line(counts) == "posted 2 · messaged 1"
    assert outward_line(None) == "unavailable (event log unreadable)"
    assert outward_line({}) == "none"


@pytest.mark.asyncio
async def test_digest_carries_the_outward_line(home, monkeypatch):
    import cron.digest as d
    monkeypatch.setattr(d, "_ledger", lambda u, days: {})
    monkeypatch.setattr(d, "_health_lines", lambda u, dd: [])
    _write("public", tool="publish", action="publish")
    text = await d.compose_digest("rob", data_dir=str(home))
    assert "• Outward: published 1" in text


def test_activity_feed_renders_the_envelope():
    from webview.activity import normalize_db_event, summarize
    row = {"id": 1, "ts": 1.0, "kind": "external_write", "effect": "social",
           "user_id": "rob", "session_id": "s",
           "attrs": '{"tool":"twitter","action":"twitter_post","target":"open",'
                    '"outcome":"denied","autonomous":true}'}
    ev = normalize_db_event("telemetry", row)
    assert ev["effect"] == "social"
    assert ev["summary"] == "social write: twitter_post → open (DENIED) [autonomous]"
    # F-8: tool_denied is emitted with action=, never tool=
    assert summarize("tool_denied", {"action": "twitter_post", "reason": "paused"}) == \
        "tool DENIED: twitter_post — paused"


def test_work_log_classes_an_external_write_by_its_effect():
    from core.activity_class import CLIENT_CLASSES, classify
    assert classify("external_write", effect="money") == "money"
    assert classify("external_write", effect="social") == "message"
    assert classify("external_write", effect="comms") == "message"
    assert classify("external_write", effect="code") == "tool"
    assert classify("external_write") == "tool"
    assert classify("external_write", effect=object()) in CLIENT_CLASSES
