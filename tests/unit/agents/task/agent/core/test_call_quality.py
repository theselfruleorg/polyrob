"""2026-10-14 review (model utilization) — the agent-step call-quality fixes.

- E-3: a tool-free step names its cause and emits ONE ``model_call_quality`` row.
- E-7: a near-miss tool name is repaired before the registry sees it.
- E-8: a truly empty completion re-issues the same call once, inside the step.
- E-2: in an autonomous run that has acted, a text-only reply becomes done(text).
- D-1: the usage-record metadata carries the session ``regime``.
"""
import asyncio
import logging
from types import SimpleNamespace
from typing import Optional

import pytest
from pydantic import BaseModel

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.core import call_quality as cq
from agents.task.agent.views import AgentOutput
from agents.task.loop_types.registry_views import ActionModel
from modules.llm.messages import AIMessage

KNOWN = ["done", "send_message", "filesystem_read_file", "filesystem_write_file",
         "wallet_balance", "web_search"]


# ── E-7: name repair ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, fixed", [
    ("done", "done"),
    ("functions.send_message", "send_message"),
    ("default_api.web_search", "web_search"),
    ("SendMessage", "send_message"),
    ("send-message", "send_message"),
    ("Web Search", "web_search"),
    ("web_search_tool", "web_search"),
    ("read_file", "filesystem_read_file"),      # the registry's own suffix rule
    ("wallet_balanse", "wallet_balance"),       # difflib, cutoff 0.7
])
def test_repair_tool_name(raw, fixed):
    assert cq.repair_tool_name(raw, KNOWN) == fixed


@pytest.mark.parametrize("raw", ["launch_rocket", "", None, "zz"])
def test_repair_tool_name_refuses_a_far_name(raw):
    assert cq.repair_tool_name(raw, KNOWN) is None


@pytest.mark.parametrize("raw", ["launchpad_claim", "email_move", "defi_trade_lp_add",
                                 "defi_trade_solana_transfer"])
def test_fuzzy_repair_never_lands_on_a_different_money_or_outbound_verb(raw):
    known = KNOWN + ["launchpad_sell", "launchpad_buy", "email_send", "email_read",
                     "defi_trade_swap", "defi_trade_transfer"]
    assert cq.repair_tool_name(raw, known) is None


def test_exact_and_wrapper_repair_onto_a_money_action_still_works():
    known = KNOWN + ["defi_trade_swap"]
    assert cq.repair_tool_name("functions.defi_trade_swap", known) == "defi_trade_swap"


class _Ctl:
    def __init__(self, errors=None):
        self._errors = errors or {}

    def list_actions(self):
        return list(KNOWN)

    def get_last_validation_errors(self):
        return dict(self._errors)


def _agent(**kw):
    a = SimpleNamespace(controller=_Ctl(), logger=logging.getLogger("t-cq"),
                        session_id="s-cq", user_id="u", llm_provider="openrouter",
                        model_name="deepseek/x", state=SimpleNamespace(n_steps=3),
                        _is_sub_agent=False, orchestrator=None)
    a.__dict__.update(kw)
    return a


@pytest.fixture
def events(monkeypatch):
    rows = []
    import core.event_log as el
    monkeypatch.setattr(el, "emit", lambda kind, **k: rows.append((kind, k)))
    return rows


def test_repair_rewrites_the_call_in_place_and_emits_repaired(events):
    calls = [{"id": "1", "name": "SendMessage", "args": {}},
             {"id": "2", "name": "done", "args": {}}]
    assert cq.repair_tool_call_names(_agent(), calls) == 1
    assert [c["name"] for c in calls] == ["send_message", "done"]
    assert [(k, r["attrs"]["issue"]) for k, r in events] == [("model_call_quality", "repaired")]


# ── E-3: the cause and the event ────────────────────────────────────────────

def test_emit_carries_the_named_keys(events):
    cq.emit_call_quality(_agent(), cq.TEXT_ONLY)
    (kind, row), = events
    assert kind == "model_call_quality" and row["source"] == "agent_step"
    attrs = row["attrs"]
    assert {k: attrs[k] for k in ("provider", "model", "issue", "session_id", "step")} == {
        "provider": "openrouter", "model": "deepseek/x", "issue": "text_only",
        "session_id": "s-cq", "step": 3}
    assert attrs["regime"] == "owner_chat"


def test_all_rejected_sets_the_step_cause_some_rejected_emits(events):
    a = _agent(controller=_Ctl(errors={"1": "not found"}))
    calls = [{"id": "1", "name": "launch_rocket", "args": {}}]
    cq.note_rejected_calls(a, calls, [])
    assert cq.take_call_issue(a) == cq.UNKNOWN_TOOL and not events
    a = _agent(controller=_Ctl(errors={"2": "Invalid arguments"}))
    calls = [{"id": "1", "name": "done", "args": {}},
             {"id": "2", "name": "web_search", "args": {"bad": 1}}]
    cq.note_rejected_calls(a, calls, ["an action"])
    assert [r["attrs"]["issue"] for _, r in events] == ["bad_args"]
    assert events[0][1]["attrs"]["partial"] is True


def test_take_call_issue_defaults_and_consumes():
    a = _agent()
    assert cq.take_call_issue(a) == cq.NO_ACTION
    cq.set_call_issue(a, cq.EMPTY)
    assert cq.take_call_issue(a) == cq.EMPTY
    assert cq.take_call_issue(a) == cq.NO_ACTION


def test_step_log_line_names_the_cause_and_emits(events, caplog):
    from agents.task.agent.core.step_execution import StepExecutionMixin

    class _MM:
        def inject_runtime_guidance(self, *a, **k):
            pass

    class _A(StepExecutionMixin):
        pass

    a = _A()
    a.__dict__.update(vars(_agent()))
    a.message_manager, a.tool_call_tracker, a.task = _MM(), None, "t"
    a.state.increment_failures = lambda: None
    a._validate_model_output = lambda mo: False
    cq.set_call_issue(a, cq.TEXT_ONLY)
    caplog.set_level(logging.WARNING, logger="t-cq")
    assert a._validate_and_intervene(None) is False
    assert any("(cause: text_only)" in r.getMessage() for r in caplog.records)
    assert [r["attrs"]["issue"] for _, r in events] == ["text_only"]


# ── D-1: regime on the usage metadata ───────────────────────────────────────

def test_usage_metadata_carries_the_regime():
    from agents.task.agent.core.next_action_internal import _prefix_stamps
    meta = _prefix_stamps(_agent(llm=None))
    assert meta["regime"] == "owner_chat"


# ── E-2: text-only = final report, autonomous only, after acting ───────────

@pytest.fixture
def cron_session():
    from core import autonomy_marker as am
    am.mark_autonomous("s-cron", cron_job_id="job-1")
    yield "s-cron"
    am._SESSIONS.pop("s-cron", None)
    am._CRON_JOBS.pop("s-cron", None)


_REPORT = "Checked the inbox: three new mails, none needs the owner. Nothing else to do."


def test_prose_is_the_final_report_in_a_cron_run_that_acted(cron_session):
    a = _agent(session_id=cron_session, _acted_this_run=True)
    call = cq.prose_final_report(a, AIMessage(content=_REPORT))
    assert call["name"] == "done" and call["args"] == {"text": _REPORT}


def test_prose_before_any_action_is_a_plan_not_a_report(cron_session):
    a = _agent(session_id=cron_session)
    assert cq.prose_final_report(a, AIMessage(content=_REPORT)) is None


def test_owner_chat_keeps_the_send_message_rule():
    a = _agent(_acted_this_run=True)
    assert cq.prose_final_report(a, AIMessage(content=_REPORT)) is None


def test_child_and_brain_json_are_not_reports(cron_session):
    a = _agent(session_id=cron_session, _acted_this_run=True, _is_sub_agent=True)
    assert cq.prose_final_report(a, AIMessage(content=_REPORT)) is None
    a = _agent(session_id=cron_session, _acted_this_run=True)
    brain = '{"current_state": {"memory": "m", "next_goal": "call the wallet tool"}}'
    assert cq.prose_final_report(a, AIMessage(content=brain)) is None
    assert cq.prose_final_report(a, AIMessage(content="ok")) is None


# ── the native path end to end: E-8 retry and E-2 conversion ────────────────

class _Done(BaseModel):
    text: str = ""


class _AM(ActionModel):
    done: Optional[_Done] = None


class _NativeCtl(_Ctl):
    def supports_native_tools(self, provider):
        return True

    def get_all_actions_for_provider(self, provider, preferred_order=None):
        return [{"type": "function", "function": {"name": n}} for n in KNOWN]

    def get_tool_schema_token_estimate(self, provider):
        return 0

    def get_action_names(self):
        return list(KNOWN)

    def tool_calls_to_actions(self, calls):
        out = []
        for c in calls:
            if c["name"] == "done":
                act = _AM(done=_Done(**c.get("args", {})))
                act._tool_call_id = c.get("id")
                out.append(act)
        return out


class _MM:
    use_native_tools = True
    _foundation_tool_names = None

    def __getattr__(self, name):  # every other read is a harmless no-op
        if name == "check_token_safety":
            return lambda *a, **k: {"safe": True, "usage_percent": 1.0,
                                    "current_tokens": 1, "max_limit": 100}
        if name in ("get_estimated_context_usage",):
            return lambda *a, **k: 0.1
        if name in ("calculate_llm_timeout",):
            return lambda *a, **k: 30
        if name in ("get_messages", "get_messages_for_llm"):
            return lambda *a, **k: []
        if name in ("get_token_count",):
            return lambda *a, **k: 0
        return lambda *a, **k: None


class _LLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def ainvoke(self, messages, **kw):
        self.calls += 1
        return self.responses.pop(0)


def _native_agent(responses, session_id="s-native", acted=False):
    from agents.task.agent.service import Agent
    a = object.__new__(Agent)
    a.logger = logging.getLogger("t-cq-native")
    a.controller = _NativeCtl()
    a.tool_call_tracker = None
    a.message_manager = _MM()
    a.model_name = "deepseek/deepseek-chat"
    a.chat_model_library = "OpenAIAdapter"
    a.use_native_tools = True
    a.use_vision = False
    a.provider_name = "fake-test-provider"
    a.llm_provider = "openrouter"
    a.state = SimpleNamespace(n_steps=2)
    a.llm = _LLM(responses)
    a.max_actions_per_step = 5
    a.usage_tracker = None
    a.telemetry_manager = None
    a.hitl_manager = SimpleNamespace(has_streaming_callbacks=lambda: False)
    a.orchestrator = SimpleNamespace(session_id=session_id, user_id=None)
    a.agent_id = "ag"
    a._is_sub_agent = False
    a.AgentOutput = AgentOutput.type_with_custom_actions(_AM)
    if acted:
        a._acted_this_run = True
    return a


def test_truly_empty_completion_is_retried_once_in_call(events):
    done = AIMessage(content="", tool_calls=[{"id": "c1", "name": "done",
                                              "args": {"text": "finished"}}])
    a = _native_agent([AIMessage(content=""), done])
    out = asyncio.run(a._get_next_action_internal([]))
    assert a.llm.calls == 2
    assert len(out.action) == 1 and out.action[0].done.text == "finished"
    assert [r["attrs"].get("in_call_retry") for _, r in events] == [True]


def test_two_empty_completions_record_the_empty_cause():
    a = _native_agent([AIMessage(content=""), AIMessage(content="")])
    out = asyncio.run(a._get_next_action_internal([]))
    assert a.llm.calls == 2 and out.action == []
    assert cq.take_call_issue(a) == cq.EMPTY


def test_text_only_in_owner_chat_is_a_text_only_step():
    a = _native_agent([AIMessage(content=_REPORT)], acted=True)
    out = asyncio.run(a._get_next_action_internal([]))
    assert out.action == []
    assert cq.take_call_issue(a) == cq.TEXT_ONLY


def test_text_only_in_a_cron_run_that_acted_becomes_done(cron_session):
    a = _native_agent([AIMessage(content=_REPORT)], session_id=cron_session, acted=True)
    out = asyncio.run(a._get_next_action_internal([]))
    assert len(out.action) == 1 and out.action[0].done.text == _REPORT
    assert a._current_normalized_tool_calls[0]["name"] == "done"


def test_a_misnamed_call_is_repaired_and_runs():
    call = AIMessage(content="", tool_calls=[{"id": "c1", "name": "functions.done",
                                              "args": {"text": "ok"}}])
    a = _native_agent([call])
    out = asyncio.run(a._get_next_action_internal([]))
    assert len(out.action) == 1 and out.action[0].done.text == "ok"


# ── D-7: the step stamps its context reading before the call ────────────────

def test_stamp_step_context_hands_the_reading_to_the_llm(monkeypatch):
    seen = []
    import modules.llm.prefix_stamp as ps
    monkeypatch.setattr(ps, "stamp_context", lambda llm, usage: seen.append((llm, usage)))
    llm = object()
    a = SimpleNamespace(llm=llm, message_manager=SimpleNamespace(context_usage=lambda: {"total": 5}))
    cq.stamp_step_context(a)
    assert seen == [(llm, {"total": 5})]
    cq.stamp_step_context(SimpleNamespace())  # fail-open
