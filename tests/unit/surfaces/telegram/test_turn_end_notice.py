"""One owner-turn end rule (prod 2026-10-26..28: 8 owner messages got no reply).

A run that ends without finishing — the step limit, a loop, consecutive failures,
an error — tells the owner once: the typed stop reason, the last tool error of
the turn, and how to resume; on a bound session too, and on the failed path. A
finished turn stays quiet. A failed turn never re-sends an earlier turn's text.
Every harness delivery is recorded in the owner thread. Never in a room.
"""
import importlib
import types

import pytest

from surfaces.telegram.harness import _run_and_deliver
from tests.unit.surfaces.telegram.test_harness import _FakeTaskAgent


def _orch(reason, cap=50, steps=None, bound=False, error=None, summary=None, spoke=False):
    result = types.SimpleNamespace(error=error, extracted_content=None)
    agent = types.SimpleNamespace(
        _is_sub_agent=False,
        # An action step: "Last tool error" quotes action results only (L1).
        history=types.SimpleNamespace(history=[types.SimpleNamespace(
            model_output=types.SimpleNamespace(action=[object()]), result=[result])]),
        state=types.SimpleNamespace(stop_reason=reason, max_steps=cap,
                                    n_steps=cap if steps is None else steps,
                                    exhausted_summary=summary))
    orch = types.SimpleNamespace(agents={"a1": agent})
    if bound:
        orch._message_router = object()
        orch._chat_session_key = "agent:main:telegram:dm:1"
    orch._spoke = spoke
    return orch


class _Agent(_FakeTaskAgent):
    def __init__(self, orch, status="Session completed successfully"):
        super().__init__(orch=orch)
        self.status = status
        self.scoped = []
        self.since = "unset"

    def scope_next_run(self, session_id, **kw):
        self.scoped.append(kw)

    def _chat_turn_baseline(self, session_id):
        return (0, [], "the PREVIOUS turn's done text")

    def _extract_chat_reply(self, session_id, since=None):
        self.since = since
        return getattr(self, "reply", "")

    async def run_session(self, user_id, session_id):
        if getattr(self._orch, "_spoke", False):
            from core.surfaces.turn_reply import mark_reply_published
            mark_reply_published(self._orch, "On it.")
        return self.status


@pytest.fixture
def thread(monkeypatch):
    rows = []
    monkeypatch.setattr("core.surfaces.owner_thread.record_owner_out",
                        lambda c, u, t, **kw: rows.append((t, kw)) or True)
    monkeypatch.setattr("core.surfaces.user_delivery.record_interactive_reply",
                        lambda *a, **k: None)
    return rows


async def _turn(ta, key="agent:main:telegram:dm:1"):
    sent = []

    async def deliver(text):
        sent.append(text)
    await _run_and_deliver(ta, "u", "s", deliver, notice_key=key)
    return sent


@pytest.mark.asyncio
async def test_a_bound_turn_that_hit_the_cap_and_said_nothing_says_why(thread):
    ta = _Agent(_orch("max_steps", error="cronjob_edit: Nothing to change", bound=True))
    sent = await _turn(ta)
    assert len(sent) == 1
    assert "stopped at the step limit (50/50)" in sent[0]
    assert "Last tool error: cronjob_edit: Nothing to change" in sent[0]
    assert "continue" in sent[0]
    assert thread and thread[0][1]["source"] == "turn_fallback" and thread[0][1]["via"] == "telegram"


@pytest.mark.asyncio
async def test_the_owner_turn_runs_on_the_interactive_budget(thread):
    from core.config_policy import interactive_max_steps; INTERACTIVE_MAX_STEPS = interactive_max_steps()
    ta = _Agent(_orch("done", bound=True))
    await _turn(ta)
    assert ta.scoped == [{"max_steps": INTERACTIVE_MAX_STEPS}]


@pytest.mark.asyncio
async def test_a_loop_stop_says_so(thread):
    sent = await _turn(_Agent(_orch("loop", steps=9, bound=True)))
    assert len(sent) == 1 and "repeated the same tool call (9/50)" in sent[0]


@pytest.mark.asyncio
async def test_a_failed_bound_turn_never_resends_the_previous_turns_text(thread):
    ta = _Agent(_orch("failures", steps=12, bound=True, error="wallet_sign_challenge: bad"),
                status="Session failed: too many failures")
    ta.reply = "the PREVIOUS turn's done text"
    sent = await _turn(ta)
    assert len(sent) == 1
    assert "PREVIOUS" not in sent[0]
    assert "too many consecutive failed steps (12/50)" in sent[0]
    assert "wallet_sign_challenge: bad" in sent[0]


@pytest.mark.asyncio
async def test_the_unbound_extraction_gets_the_turn_baseline(thread):
    ta = _Agent(_orch("done"))
    ta.reply = "the answer"
    assert await _turn(ta) == ["the answer"]
    assert ta.since == (0, [], "the PREVIOUS turn's done text")
    assert thread[0][0] == "the answer"       # the harness sent it: the thread knows


@pytest.mark.asyncio
async def test_a_finished_turn_is_quiet(thread):
    assert await _turn(_Agent(_orch("done", bound=True))) == []
    assert await _turn(_Agent(_orch("awaiting_reply", bound=True))) == []
    assert thread == []


@pytest.mark.asyncio
async def test_a_turn_that_spoke_and_was_cut_still_hears_why_but_no_report(thread):
    ta = _Agent(_orch("max_steps", bound=True, spoke=True, summary="enrolled; file written"))
    sent = await _turn(ta)
    assert len(sent) == 1 and "step limit" in sent[0]
    assert "Last report" not in sent[0]


@pytest.mark.asyncio
async def test_a_silent_capped_turn_carries_the_agents_last_report(thread):
    ta = _Agent(_orch("max_steps", bound=True, summary="Enrolled as merchant; wrote "
                                                       "data/402mob-merchant.md"))
    sent = await _turn(ta)
    assert "Last report (agent's words): Enrolled as merchant" in sent[0]


@pytest.mark.asyncio
async def test_a_room_turn_gets_no_notice(thread):
    sent = await _turn(_Agent(_orch("max_steps", bound=True)),
                       key="agent:main:telegram:group:-100123")
    assert sent == [] and thread == []


@pytest.mark.asyncio
async def test_a_no_op_status_stays_silent(thread):
    ta = _Agent(_orch("max_steps", bound=True), status="Session is already executing")
    assert await _turn(ta) == []


def test_interactive_max_steps_is_an_env_flag(monkeypatch):
    import core.config_policy.goal_flags as goal_flags
    monkeypatch.setenv("INTERACTIVE_MAX_STEPS", "120")
    assert importlib.reload(goal_flags).INTERACTIVE_MAX_STEPS == 120
    monkeypatch.setenv("INTERACTIVE_MAX_STEPS", "0")
    assert importlib.reload(goal_flags).INTERACTIVE_MAX_STEPS == 100
    monkeypatch.delenv("INTERACTIVE_MAX_STEPS")
    assert importlib.reload(goal_flags).INTERACTIVE_MAX_STEPS == 100
