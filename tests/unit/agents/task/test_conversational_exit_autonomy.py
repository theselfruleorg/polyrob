"""T2-08 (2026-07-06 structural review): conversational-exit was autonomy-blind —
only sub-agents were exempt. A goal/cron agent that emits two consecutive status
send_messages had its turn ended mid-mission (and, post-T2-01, the run landed as
BLOCKED/mislabeled) even though nobody was chatting with it.
"""
from agents.task.agent.core.conversational_exit import should_conversational_exit


def test_chat_session_still_exits():
    assert should_conversational_exit(2, False) is True


def test_sub_agent_still_exempt():
    assert should_conversational_exit(5, True) is False


def test_autonomous_run_never_conversational_exits():
    assert should_conversational_exit(5, False, is_autonomous=True) is False


def test_run_loop_resolves_autonomy_marker():
    import inspect

    from agents.task.agent.core import run_loop

    src = inspect.getsource(run_loop)
    assert "is_autonomous" in src
