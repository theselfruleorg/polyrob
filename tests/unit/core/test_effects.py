"""033: the two-layer effect classifier, the pause-kind map and the recorder."""
import sqlite3

import pytest

import core.event_log as el
from core.effects import (EFFECT_CLASSES, ENVELOPE_KEYS, GATED_EFFECTS,
                          classify_effect, effect_line, pause_kind_for,
                          record_external_write)
from core.exec_identity import reset_exec_identity, set_exec_identity


@pytest.fixture
def tlog(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    monkeypatch.delenv("EXTERNAL_WRITE_TELEMETRY", raising=False)
    el._INSTANCES.clear()
    yield lambda: el.get_event_log()
    el._INSTANCES.clear()


# --- classifier ----------------------------------------------------------------

def test_read_verb_on_a_write_tool_is_not_a_write():
    assert classify_effect("twitter", "twitter_search") is None
    assert classify_effect("git", "git_commit") is None      # local, not public


def test_unlisted_verb_on_a_write_tool_is_assumed_to_write():
    """The polarity: a new verb is covered the day it lands."""
    v = classify_effect("twitter", "twitter_brand_new_verb")
    assert (v.effect, v.confidence) == ("social", "tool")


def test_explicit_rows_report_action_confidence():
    assert classify_effect("twitter", "twitter_reply").effect == "social"
    v = classify_effect("twitter", "twitter_dm")
    assert (v.effect, v.confidence) == ("comms", "action")
    assert classify_effect("git", "git_push").effect == "public"
    assert classify_effect("self_env", "self_env_patch_source").effect == "self"
    assert classify_effect("defi_trade", "defi_trade_swap").effect == "money"


def test_money_ceiling_wins_on_a_multi_ceiling_tool():
    # x402_pay carries writes_money + writes_network; an unlisted verb is money.
    assert classify_effect("x402_pay", "x402_pay_new_verb").effect == "money"


def test_x402_sweep_is_a_read_not_money():
    """067 P0.6: x402_sweep only probes (tools/x402/discovery.py); it never pays."""
    assert classify_effect("x402_pay", "x402_pay_x402_sweep") is None
    assert classify_effect("x402_pay", "x402_pay_x402_probe") is None
    assert classify_effect("x402_pay", "x402_pay_x402_fetch").effect == "money"


def test_directly_registered_actions():
    assert classify_effect(None, "message").effect == "comms"
    assert classify_effect(None, "load_tool").effect == "self"
    assert classify_effect(None, "skill_manage").effect == "self"
    # the owner lane is user_delivery's, never an external write
    assert classify_effect(None, "send_message") is None
    assert classify_effect(None, "done") is None
    assert classify_effect(None, "some_unknown_closure") is None


def test_effect_free_tools():
    assert classify_effect("filesystem", "filesystem_write_file") is None
    assert classify_effect("web_fetch", "web_fetch_fetch_url") is None
    assert classify_effect("nonexistent_tool", "whatever") is None


def test_browser_alias_resolves_to_the_capability_row():
    assert classify_effect("browser_manager", "browser_click_element").effect == "network"
    assert classify_effect("browser", "browser_go_to_url") is None


def test_mcp_server_hint_cannot_disable_effect_gates():
    assert classify_effect("mcp", "srv_list_files").effect == "network"
    assert classify_effect("mcp", "srv_list_files", mcp_read_only=True).effect == "network"
    # a hint on a NON-mcp tool is ignored
    assert classify_effect("twitter", "twitter_post", mcp_read_only=True).effect == "social"


# --- pause kinds -----------------------------------------------------------------

def test_every_gated_effect_has_a_declared_pause_kind():
    from core.autonomy_control import KIND_SCOPES
    for eff in sorted(GATED_EFFECTS):
        assert pause_kind_for(eff) in KIND_SCOPES, eff


def test_observe_only_classes_have_no_pause_kind():
    for eff in sorted(EFFECT_CLASSES - GATED_EFFECTS):
        assert pause_kind_for(eff) is None


def test_app_service_public_writes_map_to_the_app_scope():
    assert pause_kind_for("public", "app_service") == "app_deploy"
    assert pause_kind_for("public", "publish") == "oversight_deploy"
    assert pause_kind_for("social") == "social_post"
    assert pause_kind_for("money") == "spend"


# --- recorder --------------------------------------------------------------------

def test_records_a_typed_envelope(tlog):
    record_external_write(effect="social", tool="twitter", action="twitter_post",
                          target="open", surface="cron", autonomous=True,
                          confidence="action", outcome="ok", user_id="u1",
                          session_id="s1", fingerprint="hello")
    row = tlog().query(kind="external_write")[0]
    assert row["effect"] == "social"
    assert (row["user_id"], row["session_id"]) == ("u1", "s1")
    a = row["attrs"]
    assert ENVELOPE_KEYS <= set(a)
    assert a["autonomous"] is True and a["outcome"] == "ok"
    assert len(a["dedup"]) == 16
    assert "hello" not in str(a)          # the content is never stored


def test_falls_back_to_the_ambient_tenant(tlog):
    tok = set_exec_identity("amb-u", "amb-s")
    try:
        record_external_write(effect="money", tool="defi_trade", action="x")
    finally:
        reset_exec_identity(tok)
    row = tlog().query(kind="external_write")[0]
    assert (row["user_id"], row["session_id"]) == ("amb-u", "amb-s")


def test_unknown_effect_is_dropped(tlog):
    record_external_write(effect="not_a_class", tool="t", action="a")
    assert tlog().query(kind="external_write") == []


def test_flag_off_writes_nothing(tlog, monkeypatch):
    monkeypatch.setenv("EXTERNAL_WRITE_TELEMETRY", "off")
    record_external_write(effect="social", tool="twitter", action="twitter_post")
    assert tlog().query(kind="external_write") == []


def test_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr("core.event_log.get_event_log", boom)
    record_external_write(effect="social", tool="t", action="a")


def test_count_by_effect_groups_and_filters(tlog):
    for eff, auto in (("social", True), ("social", False), ("money", True)):
        record_external_write(effect=eff, tool="t", action="a", autonomous=auto,
                              user_id="u")
    log = tlog()
    assert log.count_by_effect(user_id="u") == {"social": 2, "money": 1}
    assert log.count_by_effect(user_id="u", attrs_in={"autonomous": (1,)}) == {
        "social": 1, "money": 1}
    assert effect_line({"social": 2, "money": 1}) == "moved money 1 · posted 2"


# --- the column -------------------------------------------------------------------

def test_pre_033_db_is_migrated_in_place(tmp_path):
    p = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE telemetry_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " ts REAL NOT NULL, kind TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '',"
                 " session_id TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',"
                 " attrs TEXT NOT NULL DEFAULT '{}')")
    conn.execute("INSERT INTO telemetry_events (ts, kind, user_id, attrs)"
                 " VALUES (1.0, 'social_write', 'u', '{}')")
    conn.commit()
    conn.close()
    log = el.TelemetryEventLog(str(p))
    rows = log.query(kind="social_write")
    assert len(rows) == 1 and rows[0]["effect"] == ""
    log.record("external_write", user_id="u", effect="social")
    assert len(log.query(effect="social")) == 1


def test_unmigratable_file_still_reads(tmp_path, monkeypatch):
    """A pre-033 file the migration cannot alter keeps every legacy read working."""
    p = tmp_path / "ro.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE telemetry_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " ts REAL NOT NULL, kind TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '',"
                 " session_id TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',"
                 " attrs TEXT NOT NULL DEFAULT '{}')")
    conn.execute("INSERT INTO telemetry_events (ts, kind) VALUES (1.0, 'cron_run')")
    conn.commit()
    conn.close()

    def refuse(_path):
        raise sqlite3.OperationalError("attempt to write a readonly database")
    monkeypatch.setattr(el, "_migrate_effect_column", refuse)
    log = el.TelemetryEventLog(str(p))
    assert [r["kind"] for r in log.query()] == ["cron_run"]
    assert log.query(effect="social") == []
    assert log.count_by_effect() is None      # unknown, never a confident zero
