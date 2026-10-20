"""083 WS-5: one step-budget notice at 80 %, on the ephemeral rail."""
from types import SimpleNamespace

from agents.task.agent.core.budget_notice import maybe_push_budget_notice


class _MM:
	def __init__(self, fail=False):
		self.pushed = []
		self.fail = fail

	def push_ephemeral_message(self, message):
		if self.fail:
			raise RuntimeError("rail down")
		self.pushed.append(message)


def _agent(sub=False, fail=False):
	return SimpleNamespace(message_manager=_MM(fail), _is_sub_agent=sub,
						   session_id="chat-session", _budget_notice_sent=False)


def _run(agent, max_steps):
	fired = []
	for step_num in range(max_steps):
		if maybe_push_budget_notice(agent, step_num, max_steps):
			fired.append(step_num)
	return fired


def test_fires_once_at_the_80_percent_step():
	agent = _agent(sub=True)
	assert _run(agent, 10) == [7]  # before step 8 of 10
	assert len(agent.message_manager.pushed) == 1
	text = str(agent.message_manager.pushed[0].content)
	assert "You have used 7 of 10 steps." in text
	assert "do not stop because of this notice" in text


def test_never_twice_in_one_run():
	agent = _agent(sub=True)
	_run(agent, 150)
	assert not maybe_push_budget_notice(agent, 149, 150)
	assert len(agent.message_manager.pushed) == 1


def test_never_when_the_cap_is_four_or_less():
	for cap in (1, 2, 3, 4):
		agent = _agent(sub=True)
		assert _run(agent, cap) == []
		assert agent.message_manager.pushed == []


def test_a_chat_run_gets_no_report_sentence(monkeypatch):
	monkeypatch.setattr(
		"agents.task.agent.core.exhaustion_summary.summary_turn_applies", lambda a: False)
	agent = _agent()
	_run(agent, 8)
	text = str(agent.message_manager.pushed[0].content)
	assert "one turn to report" not in text


def test_an_autonomous_or_sub_agent_run_gets_the_report_sentence(monkeypatch):
	agent = _agent(sub=True)
	_run(agent, 20)
	assert "At the limit you get one turn to report." in str(
		agent.message_manager.pushed[0].content)

	monkeypatch.setattr(
		"agents.task.session_class.budget_class_autonomous", lambda sid: True)
	agent = _agent(sub=False)
	_run(agent, 20)
	assert "one turn to report" in str(agent.message_manager.pushed[0].content)


def test_a_push_failure_does_not_break_the_step():
	agent = _agent(sub=True, fail=True)
	assert maybe_push_budget_notice(agent, 8, 10) is False  # no raise


def test_the_notice_rides_the_guidance_origin():
	from modules.llm.messages import MessageOrigin
	agent = _agent(sub=True)
	_run(agent, 10)
	assert agent.message_manager.pushed[0].origin == MessageOrigin.GUIDANCE
