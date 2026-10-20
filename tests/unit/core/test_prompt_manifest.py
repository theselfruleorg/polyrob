"""2026-10-14 review D-10 — one `prompt_manifest` event per session start."""
import pytest

from core import prompt_manifest as PM


@pytest.fixture
def log(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    test_log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: test_log)
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)
    monkeypatch.setattr(PM, "_SEEN", set())
    return test_log


def test_build_keeps_sizes_and_drops_unmeasured():
    m = PM.build_prompt_manifest(regime="cron", slots={"foundation": 100, "tools": 50,
                                                       "bad": "x", "flag": True},
                                 skills=["exits", "exits", "pre-trade-check"], recall_rows=0)
    assert m["slots"] == {"foundation": 100, "tools": 50} and m["total_tokens"] == 150
    assert m["skills"] == ["exits", "pre-trade-check"] and m["recall_rows"] == 0
    assert "previous_runs_chars" not in m and "live_health_chars" not in m


def test_emits_once_per_session(log):
    assert PM.emit_prompt_manifest(session_id="s1", user_id="rob", regime="cron",
                                   slots={"foundation": 10}, previous_runs_chars=7)
    assert not PM.emit_prompt_manifest(session_id="s1", user_id="rob")
    assert PM.emit_prompt_manifest(session_id="s1", phase="compaction")
    rows = log.query(kind="prompt_manifest")
    assert len(rows) == 2
    first = [r for r in rows if r["attrs"]["phase"] == "start"][0]
    assert first["session_id"] == "s1" and first["attrs"]["previous_runs_chars"] == 7
    assert not PM.emit_prompt_manifest(session_id="")


def test_agent_first_step_emits_the_manifest(log):
    import types
    from agents.task.agent.core import logging_io
    cls = [v for v in vars(logging_io).values()
           if isinstance(v, type) and hasattr(v, "_emit_prompt_manifest")][0]
    agent = types.SimpleNamespace(
        session_id="s9", user_id="rob", llm=types.SimpleNamespace(model_name="m1"),
        controller=types.SimpleNamespace(_activated_skills={"exits", "x#ack"}),
        task="<previous-runs>\nrun 1: SKIPPED\n</previous-runs>\nwatch it",
        logger=types.SimpleNamespace(debug=lambda *a, **k: None))
    cls._emit_prompt_manifest(agent, {"foundation": 30000, "tool_schemas": 50000})
    (row,) = log.query(kind="prompt_manifest")
    a = row["attrs"]
    assert row["session_id"] == "s9" and a["model"] == "m1" and a["skills"] == ["exits"]
    assert a["slots"]["tool_schemas"] == 50000 and a["previous_runs_chars"] > 20


def _mm_stub(**over):
    """A MessageManager stand-in: the ONE gauge plus the layer token attrs."""
    import types
    from modules.llm.messages import MessageOrigin
    usage = {"slots": {"foundation": 30000, "conversation": 10, "ephemeral": 0,
                       "hmem": 0, "tool_schemas": 52000},
             "last_call": {"prompt_tokens": 83000, "cached_tokens": 4000}}
    usage.update(over)
    msg = lambda origin: types.SimpleNamespace(origin=origin, content="x" * 400)
    return types.SimpleNamespace(
        context_usage=lambda: usage,
        _ephemeral_messages=[msg(MessageOrigin.RECALL)],
        _ephemeral_pending=[msg(MessageOrigin.RECALL), msg(None)],
        estimate_tokens=lambda msgs: 100 * len(msgs),
        _system_message_tokens=21000, _tool_catalog_tokens=900, _skill_message_tokens=0)


def _agent(n_steps, mm):
    import types
    return types.SimpleNamespace(
        session_id="s10", user_id="rob", llm=types.SimpleNamespace(model_name="m1"),
        controller=None, task="hi", state=types.SimpleNamespace(n_steps=n_steps),
        message_manager=mm, logger=types.SimpleNamespace(debug=lambda *a, **k: None))


def _mixin():
    from agents.task.agent.core.logging_io import LoggingIOMixin
    return LoggingIOMixin


def test_first_call_row_carries_schemas_step1_blocks_layers_and_provider_count(log):
    """081 WS-0.2: the row is written after the first call, so tool_schemas is
    the real cost (not 0), the consumed step-1 blocks count, each foundation
    layer has its size, and the provider's prompt_tokens rides along."""
    agent = _agent(1, _mm_stub())
    agent._prompt_manifest_measure = lambda: _mixin()._prompt_manifest_measure(agent)
    _mixin()._emit_prompt_manifest(agent)
    (row,) = log.query(kind="prompt_manifest")
    a = row["attrs"]
    assert a["slots"]["tool_schemas"] == 52000
    assert a["slots"]["ephemeral"] == 300
    assert a["ephemeral"] == {"recall": 200, "untagged": 100}
    assert a["layers"] == {"system_prompt": 21000, "tool_catalog": 900}
    assert a["prompt_tokens"] == 83000 and a["cached_tokens"] == 4000


def test_manifest_is_a_no_op_after_step_one(log):
    agent = _agent(2, _mm_stub())
    agent._prompt_manifest_measure = lambda: _mixin()._prompt_manifest_measure(agent)
    _mixin()._emit_prompt_manifest(agent)
    assert log.query(kind="prompt_manifest") == []


def test_no_provider_usage_means_no_prompt_tokens_key(log):
    agent = _agent(1, _mm_stub(last_call={}))
    agent._prompt_manifest_measure = lambda: _mixin()._prompt_manifest_measure(agent)
    _mixin()._emit_prompt_manifest(agent)
    (row,) = log.query(kind="prompt_manifest")
    assert "prompt_tokens" not in row["attrs"]


def test_the_emit_sits_after_the_first_call_not_in_the_gauge():
    """The gauge at the top of step 1 runs before the schemas and the step-1
    blocks exist; the manifest call lives in ``_call_llm`` after the call."""
    import inspect
    from agents.task.agent.core import logging_io, step
    assert "_emit_prompt_manifest" not in inspect.getsource(
        logging_io.LoggingIOMixin._log_context_breakdown)
    src = inspect.getsource(step)
    call = src.index("async def _call_llm")
    assert src.index("emit_manifest()", call) > src.index(
        "await self.get_next_action(input_messages)", call)


def test_the_row_names_the_gap_between_the_estimate_and_the_provider():
    """081 re-measure: the slots are estimates (owner row 14K under a 156K call);
    the row carries the signed gap so a reader sees it without a second query."""
    from core.prompt_manifest import build_prompt_manifest
    row = build_prompt_manifest(slots={"foundation": 100, "tool_schemas": 50},
                                extra={"prompt_tokens": 170})
    assert row["total_tokens"] == 150 and row["estimate_gap"] == 20
    assert "estimate_gap" not in build_prompt_manifest(slots={"foundation": 1})
