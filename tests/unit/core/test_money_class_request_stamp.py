"""112 F2/F2b: the goal and cron runners stamp ``money_class`` on the run request,
and ``SessionRequest`` carries it (default False)."""
from agents.task.task_agent_support import SessionRequest
from core.money.classify import is_money_class


def test_session_request_carries_money_class():
    assert SessionRequest(task="t", provider="openai", model="m").money_class is False
    assert SessionRequest(task="t", provider="openai", model="m", money_class=True).money_class


def test_priority_money_on_a_non_money_rig_is_money_class():
    assert is_money_class({"priority": "money", "rig": "social"})
    assert not is_money_class({"rig": "social"})


def test_runners_stamp_the_request():
    import inspect
    import cron.runner as runner
    import agents.task.goals.goal_run as goal_run
    for mod in (runner, goal_run):
        assert '"money_class": _money_class(payload)' in inspect.getsource(mod)
