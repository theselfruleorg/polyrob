"""080 WS-5: `<live-health>` names `agent_status` only where the tool exists."""
from types import SimpleNamespace

from agents.task.agent.core.live_health import has_status_tool
from core.status_render import render_agent_health_note
from core.status_snapshot import build_status_snapshot


def _many_items_snap(tmp_path):
    snap = build_status_snapshot("owner", data_dir=str(tmp_path), include_money=False)
    item = snap.health[0] if snap.health else None
    if item is None:
        from core.status_types import HealthItem, SEVERITY_WARN
        item = HealthItem(key="x", severity=SEVERITY_WARN, text="x")
    snap.health[:] = [item] * 12
    return snap


def test_note_names_agent_status_only_when_present(tmp_path):
    snap = _many_items_snap(tmp_path)
    assert "call agent_status" in render_agent_health_note(snap, status_tool=True)
    note = render_agent_health_note(snap, status_tool=False)
    assert "call agent_status" not in note
    assert "more (" in note


def test_has_status_tool_reads_the_registry():
    reg = SimpleNamespace(registry=SimpleNamespace(actions={"agent_status": object()}))
    assert has_status_tool(SimpleNamespace(registry=reg)) is True
    empty = SimpleNamespace(registry=SimpleNamespace(actions={}))
    assert has_status_tool(SimpleNamespace(registry=empty)) is False
    assert has_status_tool(None) is False
