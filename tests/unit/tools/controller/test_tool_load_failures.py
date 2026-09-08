"""FIX 4 — a requested tool that never registers must not vanish silently.

`load_tools_from_container` skipped any requested tool_id the container could not
serve: no exception, no warning that reached the run, nothing on the goal row.
`data/streams/streams.yaml` grants the `ship-software` stream `publish`
unconditionally while prod runs with `PUBLISH_ENABLED` off, so the seeded goal
dispatched WITHOUT `publish` and neither the model nor the owner was ever told
why the deliverable had no URL.

The omission is now recorded per-controller with the SAME `gated:<reason>`
vocabulary `tools/tool_disclosure.py` already renders, and the goal dispatcher
reads it back onto the run (see
tests/unit/agents/task/goals/test_dispatcher_tool_gap.py).
"""
import types

import pytest


class _Tool:
    is_initialized = True


class _Container:
    """Serves exactly the named services (the three probes the loader uses)."""

    def __init__(self, services):
        self._s = dict(services)
        self.config = types.SimpleNamespace(data_dir="/tmp")

    def has_service(self, name):
        return name in self._s

    def get_service(self, name):
        return self._s.get(name)


def _controller(container):
    import agents.task.agent.service  # noqa: F401 — controller<->orchestrator cycle
    from tools.controller.service import Controller

    orch = types.SimpleNamespace(session_id="s1", user_id="u1", workspace_dir="/tmp")
    return Controller(container=container, orchestrator=orch)


@pytest.mark.asyncio
async def test_a_missing_tool_is_recorded_with_a_reason(monkeypatch):
    monkeypatch.delenv("PUBLISH_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_BUILDER_MODE", "off")
    c = _controller(_Container({"filesystem_tool": _Tool()}))

    loaded = await c.load_tools_from_container(["filesystem", "publish"])

    assert "filesystem" in loaded and "publish" not in loaded, \
        "which tools LOAD must not change"
    failures = c.get_tool_load_failures()
    assert "publish" in failures
    assert "PUBLISH_ENABLED" in failures["publish"], \
        "name the gate when it is derivable, not just 'not found'"
    assert failures["publish"].startswith("gated:"), \
        "reuse the tool_disclosure gated:<reason> vocabulary"


@pytest.mark.asyncio
async def test_a_loaded_tool_is_never_reported_as_a_gap():
    c = _controller(_Container({"filesystem_tool": _Tool()}))
    await c.load_tools_from_container(["filesystem"])
    assert c.get_tool_load_failures() == {}


@pytest.mark.asyncio
async def test_a_later_successful_load_clears_the_gap():
    container = _Container({})
    c = _controller(container)
    await c.load_tools_from_container(["filesystem"])
    assert "filesystem" in c.get_tool_load_failures()

    container._s["filesystem_tool"] = _Tool()      # e.g. a later load_tool() call
    await c.load_tools_from_container(["filesystem"])
    assert c.get_tool_load_failures() == {}


@pytest.mark.asyncio
async def test_an_unknown_tool_id_still_gets_an_honest_reason():
    c = _controller(_Container({}))
    await c.load_tools_from_container(["not_a_real_tool"])
    reason = c.get_tool_load_failures()["not_a_real_tool"]
    assert reason.startswith("gated:") and "unknown-tool" in reason


def test_every_mapped_gate_flag_is_a_real_flag():
    """The tool->flag map must never name a flag that does not exist."""
    from core.flags_catalog import CATALOG
    from tools.controller.tool_load_report import TOOL_GATE_FLAGS

    known = {row[0] for row in CATALOG}
    unknown = {f for f in TOOL_GATE_FLAGS.values() if f not in known}
    assert not unknown, f"tool_load_report names unknown flags: {sorted(unknown)}"


def test_gap_note_is_empty_without_gaps():
    from tools.controller.tool_load_report import format_tool_gap_note
    assert format_tool_gap_note({}) == ""
    note = format_tool_gap_note({"publish": "gated:disabled-by-flag — PUBLISH_ENABLED is off"})
    assert "publish" in note and "PUBLISH_ENABLED" in note
