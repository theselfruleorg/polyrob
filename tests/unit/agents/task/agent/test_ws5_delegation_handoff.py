"""2026-10-13 review D1/D2/D4: a delegation hand-off keeps the data."""
import asyncio
import types

from agents.task.agent.sub_agent_manager import SubAgentManager, SubAgentOutput, SubAgentResult


# --- D2: the child's done() report is not cut to 500 chars --------------------

def test_the_done_report_reaches_the_parent_in_full(monkeypatch):
    monkeypatch.delenv("TOOL_RESULT_MAX_TOKENS", raising=False)
    report = "R" * 3000 + " END"
    out = SubAgentOutput(summary="Extracted 1 data items", extracted_content=[report],
                         final_text=report)
    text = out.to_string(max_length=2000)
    assert report in text
    assert text.count("R" * 3000) == 1, "the report is not repeated as Content 1"


def test_an_over_cap_report_names_its_file(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "100")
    out = SubAgentOutput(final_text="x" * 2000, report_file="delegation_report_c_1.md")
    text = out.to_string()
    assert "x" * 400 in text and "x" * 401 not in text
    assert 'file_path="delegation_report_c_1.md"' in text


# --- D1: a background child stopped at its step limit is not "completed" ------

def test_a_stopped_background_child_is_not_reported_completed():
    import agents.task.agent.async_delegation as ad

    delivered = []
    mgr = ad.AsyncDelegationRegistry.__new__(ad.AsyncDelegationRegistry)
    result = SubAgentResult(agent_id="c", task="t", output=SubAgentOutput(raw_text="half"),
                            success=True, finished=False, steps_taken=3, max_steps=3,
                            stop_reason="max_steps")

    class _M:
        async def run_subtask(self, **kw):
            return result

    mgr._manager = _M()
    mgr._store = None
    mgr._clock = lambda: 1.0
    mgr._session_id = "s"
    mgr._emit_delegation_event = lambda *a, **k: None

    async def _deliver(rec, block):
        delivered.append(block)
    mgr._deliver = _deliver
    rec = types.SimpleNamespace(delegation_id="d1", goal="t", status="running",
                                completed_at=None, dispatched_at=0.0)
    asyncio.run(mgr._run_and_deliver(rec, goal="t", profile="executor",
                                     parent_agent_id="p", caller_is_sub=False))
    assert rec.status == "incomplete"
    assert "✅" not in delivered[0] and "STOPPED before it finished" in delivered[0]
    # 083 WS-1: the block names the real stop reason, not a fixed guess.
    assert "the child stopped at the step limit (3/3)" in delivered[0]


# --- D4: the child gets a dated brief of the owner's latest ask ---------------

def _mgr(last_ask):
    mgr = SubAgentManager.__new__(SubAgentManager)
    mgr.logger = types.SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None,
                                       warning=lambda *a, **k: None)
    mgr.session_id = "sess-1"

    class _PM:
        def get_workspace_dir(self, session_id, user_id):
            raise RuntimeError("no workspace")

    parent = types.SimpleNamespace(
        task="the first message of the chat, days ago",
        state=types.SimpleNamespace(last_result=[types.SimpleNamespace(
            extracted_content="unrelated previous step output")]))
    mgr.orchestrator = types.SimpleNamespace(session_id="sess-1", user_id="u1",
                                             agents={"parent_sess-1": parent},
                                             _path_manager=_PM(), _last_owner_ask=last_ask)
    return mgr


def test_the_child_gets_the_latest_owner_ask_dated():
    out = _mgr(("compare the two wallets and pick the cheaper bridge", 1_760_000_000.0)) \
        ._gather_parent_context("parent")
    assert "compare the two wallets" in out
    assert "UTC" in out
    assert "unrelated previous step output" not in out
    assert "the first message of the chat" not in out


def test_without_an_owner_ask_the_parent_task_is_not_called_the_owners():
    out = _mgr(None)._gather_parent_context("parent")
    assert "the first message of the chat" in out
    assert "The parent session's task" in out
    assert "owner's latest ask" not in out


# --- D3: the sync result renders the output, not a dataclass repr -------------

def test_the_sync_result_does_not_interpolate_a_dataclass_repr():
    import inspect
    import tools.controller.action_registration as ar
    src = inspect.getsource(ar)
    assert "{result.output}" not in src
    r = SubAgentResult(agent_id="c", task="t", success=True,
                       output=SubAgentOutput(final_text="the answer"))
    assert "SubAgentOutput(" not in r.output_text and "the answer" in r.output_text
