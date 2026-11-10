"""T4-02 (2026-07-06 structural review): memory writes and recalls were invisible
on every surface — producers logged only at debug, no event kind existed, and
recall was injected as an ephemeral LLM message no owner surface ever showed.

First-class `memory_recall` / `memory_write` events now ride the durable event
log (telemetry_events.db → `/telemetry` CLI + webview `/activity`), carrying
scope, chars, a ≤120-char secret-scrubbed preview, and the producing source.
"""
import asyncio

import pytest

from core.event_log import TelemetryEventLog
from agents.task.telemetry.memory_events import emit_memory_event, scrubbed_preview


@pytest.fixture()
def log(tmp_path, monkeypatch):
    lg = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    monkeypatch.setattr(
        "core.event_log.get_event_log", lambda db_path=None: lg
    )
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_ENABLED", raising=False)
    return lg


# ------------------------------------------------------------------ helper

def test_scrubbed_preview_caps_and_redacts():
    secret = "api key sk-ant-api03-" + "a" * 80 + " trailing context " + "x" * 200
    p = scrubbed_preview(secret)
    assert len(p) <= 120
    assert "sk-ant-api03-" + "a" * 80 not in p


def test_emit_memory_event_records_row(log):
    emit_memory_event(
        "memory_write", user_id="u1", session_id="s1", source="sync_turn",
        scope="cross_session", content="TechCrunch lists 30 AI startups", count=3,
    )
    rows = log.query(kind="memory_write")
    assert len(rows) == 1
    r = rows[0]
    assert r["user_id"] == "u1" and r["session_id"] == "s1"
    assert r["source"] == "sync_turn"
    assert r["attrs"]["scope"] == "cross_session"
    assert r["attrs"]["chars"] == len("TechCrunch lists 30 AI startups")
    assert "TechCrunch" in r["attrs"]["preview"]
    assert r["attrs"]["count"] == 3


# ------------------------------------------------------------------ recall

def test_prefetch_emits_memory_recall_event(log, monkeypatch):
    async def fake_prefetch(query, *, session_id=None, user_id=None, **_kw):
        return "Recalled: the goal DB lives at data/goals.db"

    monkeypatch.setattr("modules.memory.registry.memory_prefetch", fake_prefetch)
    monkeypatch.setenv("KB_AUTO_PREFETCH", "false")

    from agents.task.agent.core.memory_prefetch import build_prefetch_message

    msg = asyncio.new_event_loop().run_until_complete(
        build_prefetch_message("where is the goal db", session_id="s9", user_id="u9")
    )
    assert msg is not None
    rows = log.query(kind="memory_recall")
    assert len(rows) == 1
    r = rows[0]
    assert r["session_id"] == "s9" and r["user_id"] == "u9"
    assert r["source"] == "prefetch"
    assert "goal DB" in r["attrs"]["preview"]
    # preview must be the RAW recall, not the untrusted-wrap envelope
    assert "untrusted_tool_result" not in r["attrs"]["preview"]


def test_prefetch_no_event_when_nothing_recalled(log, monkeypatch):
    async def fake_prefetch(query, *, session_id=None, user_id=None, **_kw):
        return ""

    monkeypatch.setattr("modules.memory.registry.memory_prefetch", fake_prefetch)
    monkeypatch.setenv("KB_AUTO_PREFETCH", "false")

    from agents.task.agent.core.memory_prefetch import build_prefetch_message

    msg = asyncio.new_event_loop().run_until_complete(
        build_prefetch_message("anything", session_id="s9", user_id="u9")
    )
    assert msg is None
    assert log.query(kind="memory_recall") == []


def _external_provider(monkeypatch, *, external=True):
    from types import SimpleNamespace
    monkeypatch.setattr("modules.memory.registry.get_memory_registry",
                        lambda: SimpleNamespace(active=lambda: SimpleNamespace(is_external=external)))


def _prefetch(monkeypatch, recalled, **kw):
    async def fake_prefetch(query, *, session_id=None, user_id=None, **_kw):
        return recalled

    monkeypatch.setattr("modules.memory.registry.memory_prefetch", fake_prefetch)
    monkeypatch.setenv("KB_AUTO_PREFETCH", "false")
    from agents.task.agent.core.memory_prefetch import build_prefetch_message
    return asyncio.new_event_loop().run_until_complete(
        build_prefetch_message("anything at all", session_id="s9", user_id="u9", **kw))


def test_an_empty_recall_is_its_own_kind(log, monkeypatch):
    _external_provider(monkeypatch)
    assert _prefetch(monkeypatch, "") is None
    rows = log.query(kind="memory_recall_empty")
    assert len(rows) == 1
    assert rows[0]["attrs"]["query"] == "anything at all"
    assert rows[0]["attrs"]["seen_only"] is False
    assert rows[0]["source"] == "prefetch"
    assert log.query(kind="memory_recall") == [], "a count of memory_recall stays a count of injections"


def test_a_recall_this_run_already_showed_is_seen_only(log, monkeypatch):
    _external_provider(monkeypatch)
    seen = set()
    assert _prefetch(monkeypatch, "- a recalled row", seen=seen) is not None
    assert _prefetch(monkeypatch, "- a recalled row", seen=seen) is None
    assert log.query(kind="memory_recall_empty")[0]["attrs"]["seen_only"] is True
    assert len(log.query(kind="memory_recall")) == 1


def test_no_store_records_no_empty_recall(log, monkeypatch):
    _external_provider(monkeypatch, external=False)
    assert _prefetch(monkeypatch, "") is None
    assert log.query(kind="memory_recall_empty") == []


def test_the_status_snapshot_skips_the_empty_recall_rows(tmp_path):
    """Like memory_recall: bulk with no health signal, and the read is a capped window."""
    import time
    from core.status_snapshot import _read_telemetry, _telemetry_db_path
    home = str(tmp_path)
    import os
    path = _telemetry_db_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lg = TelemetryEventLog(path)
    lg.record("memory_recall_empty", user_id="u", source="prefetch")
    lg.record("cron_run", user_id="u", source="cron")
    counts = _read_telemetry("u", home, time.time() - 60).data["counts"]
    assert "memory_recall_empty" not in counts and counts.get("cron_run") == 1


# ------------------------------------------------------------------ curated write

def test_memory_tool_add_emits_memory_write(log, monkeypatch, tmp_path):
    import logging
    import types

    import agents.task.agent.service  # noqa: F401 — import-cycle guard
    from tools.controller.registry.service import Registry
    from tools.controller.service import Controller

    class _Prov:
        is_external = True

        async def curated_read(self, user_id):
            return ""

        async def curated_add(self, user_id, content):
            return True

        async def curated_remove(self, user_id, content):
            return 1

        async def note_create(self, user_id, content, *, title=None, tags=None,
                               source=None, created_by="agent", status="active"):
            return 1

    monkeypatch.setenv("MEMORY_TOOL_ENABLED", "true")
    monkeypatch.setattr(
        "modules.memory.registry.get_memory_registry",
        lambda: types.SimpleNamespace(active=lambda: _Prov()),
    )

    c = object.__new__(Controller)
    c.logger = logging.getLogger("memory-events-test")
    c.registry = Registry()
    c.user_id = "u1"
    c.session_id = "s1"
    c._register_memory_tool_action()

    action = c.registry.registry.actions["memory"]
    ctx = types.SimpleNamespace(user_id="u1", session_id="s1")
    params = action.param_model(action="add", content="prefers dark mode")
    res = asyncio.new_event_loop().run_until_complete(
        action.function(params, execution_context=ctx)
    )
    assert res.error is None
    rows = log.query(kind="memory_write")
    assert len(rows) == 1
    assert rows[0]["attrs"]["scope"] == "curated"
    assert rows[0]["source"] == "memory_tool"
    assert "dark mode" in rows[0]["attrs"]["preview"]


# ------------------------------------------------------------------ CLI specs

def test_cli_registers_memory_event_specs():
    import cli.ui.events  # noqa: F401 — registration happens on import

    from cli.ui.event_registry import get_spec

    for kind in ("memory_recall", "memory_write"):
        spec = get_spec(kind)
        assert spec is not None, kind
        ev = spec.parse({"type": kind, "data": {"scope": "cross_session",
                                                "chars": 42, "preview": "p"}})
        line = spec.render_line(ev) if spec.render_line else None
        assert line and "memor" in line.lower()
