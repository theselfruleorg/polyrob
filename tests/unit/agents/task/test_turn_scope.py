"""A wake narrows ONE run; the owner turn owns its budget (prod 2026-10-28, 82b74d81).

A card wake into a finished, non-resident owner chat used to write
``task='[cards-reply] …'`` and ``max_steps=8`` into the durable request, and every
later owner turn of that chat ran with 8 steps. The narrowing is now a turn scope:
the woken run gets it, the stored request never does, and an owner turn runs on
``INTERACTIVE_MAX_STEPS`` whatever the stored request says.
"""
from types import SimpleNamespace

from agents.task.constants import CHAT_MAX_STEPS
from agents.task.task_agent_delivery import TaskAgentDeliveryMixin, is_narrowed_task
from core.config_policy import interactive_max_steps

INTERACTIVE_MAX_STEPS = interactive_max_steps()

S = "sess-owner"
OWNER_TASK = "we started fresh conversation please check now"


def _agent(request):
    class _A(TaskAgentDeliveryMixin):
        pass

    a = _A()
    info = {"user_id": "alice", "status": "failed", "request": dict(request)}
    writes = []

    class SM:
        def get_session_info(self, sid):
            return info

        def update_session_request(self, sid, req):
            writes.append(dict(req))
            info["request"] = dict(req)

    class Reg:
        def get(self, sid):
            return None          # not resident: true after any restart

    a.session_manager, a._registry = SM(), Reg()
    return a, info, writes


def _resident_agent(task):
    return SimpleNamespace(task=task, message_manager=SimpleNamespace(task=task))


def test_a_card_wake_narrows_the_woken_run_and_never_the_stored_request():
    a, info, writes = _agent({"task": OWNER_TASK, "max_steps": INTERACTIVE_MAX_STEPS})
    recreate_from = a._narrow_finished_session_task(S, info, "cards_settled")
    assert recreate_from["request"]["task"].startswith("[cards-reply]")
    assert OWNER_TASK in recreate_from["request"]["task"]           # context only
    assert writes == []                                             # nothing persisted
    assert info["request"] == {"task": OWNER_TASK, "max_steps": INTERACTIVE_MAX_STEPS}
    # the woken run: the chat-turn budget
    woken = _resident_agent(recreate_from["request"]["task"])
    assert a._run_max_steps(S, info["request"], woken) == CHAT_MAX_STEPS
    assert is_narrowed_task(woken.task)                             # kept for that run


def test_the_owner_turn_after_a_card_wake_runs_on_the_interactive_budget_with_its_task():
    a, info, _ = _agent({"task": OWNER_TASK, "max_steps": INTERACTIVE_MAX_STEPS})
    recreate_from = a._narrow_finished_session_task(S, info, "cards_settled")
    agent = _resident_agent(recreate_from["request"]["task"])
    a._run_max_steps(S, info["request"], agent)                     # the woken run
    # the owner writes next: the harness scopes the turn, the agent stays resident
    a.scope_next_run(S, max_steps=INTERACTIVE_MAX_STEPS)
    assert a._run_max_steps(S, info["request"], agent) == INTERACTIVE_MAX_STEPS
    assert agent.task == OWNER_TASK and agent.message_manager.task == OWNER_TASK


def test_an_owner_turn_ignores_a_stored_clamp():
    a, info, _ = _agent({"task": OWNER_TASK, "max_steps": "8"})
    a.scope_next_run(S, max_steps=INTERACTIVE_MAX_STEPS)
    assert a._run_max_steps(S, info["request"]) == INTERACTIVE_MAX_STEPS
    # the scope is taken once: a run with no scope reads the stored value
    assert a._run_max_steps(S, info["request"]) == 8


def test_a_request_an_older_build_narrowed_heals_on_recreate():
    """Prod 82b74d81: request.max_steps='8', task='[cards-reply]…', original_task set."""
    a, info, writes = _agent({"task": "[cards-reply] The owner decided the cards you "
                                      "raised; … The original request, for context only: x",
                              "max_steps": "8", "original_task": OWNER_TASK})
    a._heal_narrowed_request(S)
    assert info["request"]["task"] == OWNER_TASK
    assert "max_steps" not in info["request"] and "original_task" not in info["request"]
    assert len(writes) == 1
    assert a._run_max_steps(S, info["request"]) == INTERACTIVE_MAX_STEPS


def test_a_plain_request_is_never_rewritten_by_the_heal():
    a, info, writes = _agent({"task": OWNER_TASK, "max_steps": 30})
    a._heal_narrowed_request(S)
    assert writes == [] and info["request"]["max_steps"] == 30


def test_a_wake_into_a_legacy_narrowed_row_keeps_the_owner_task_as_context():
    a, info, writes = _agent({"task": "[cards-reply] old narrowed text", "max_steps": 8,
                              "original_task": OWNER_TASK})
    recreate_from = a._narrow_finished_session_task(S, info, "cards_settled")
    assert recreate_from["request"]["original_task"] == OWNER_TASK
    assert OWNER_TASK in recreate_from["request"]["task"]
    assert writes == []


# --- review M4: a run that never ran leaves no scope behind --------------------

def _task_agent_with_status(status):
    import asyncio
    from agents.task_agent_lite import TaskAgent

    ta = object.__new__(TaskAgent)
    info = {"user_id": "alice", "status": status,
            "request": {"task": OWNER_TASK, "max_steps": 30}}

    class SM:
        def get_session_info(self, sid):
            return info

        def try_transition_status(self, *a):
            return False

        def get_active_sessions(self, uid):
            return [S]

    ta.task_available = True
    ta.session_manager = SM()
    ta._session_execution_locks = {S: asyncio.Lock()}
    ta._container = None
    return ta, info


def test_an_already_executing_run_drops_the_scope_it_was_given():
    import asyncio
    ta, info = _task_agent_with_status("running")
    ta.scope_next_run(S, max_steps=INTERACTIVE_MAX_STEPS)
    assert asyncio.run(ta.run_session("alice", S)) == "Session is already executing"
    assert ta.pending_scope(S) is None
    # the session's next run (a correspondent reply sets no scope): the stored budget
    assert ta._run_max_steps(S, info["request"]) == 30


def test_a_scope_set_by_a_later_caller_survives_an_earlier_no_op_run():
    ta, _ = _task_agent_with_status("running")
    ta.scope_next_run(S, task="[cards-reply] x", max_steps=CHAT_MAX_STEPS)
    mine = ta.pending_scope(S)
    ta.scope_next_run(S, max_steps=INTERACTIVE_MAX_STEPS)        # the owner turn
    ta.drop_untaken_scope(S, mine)                                # the wake's no-op returns
    assert ta.pending_scope(S) == {"max_steps": INTERACTIVE_MAX_STEPS}   # no wake task merged
