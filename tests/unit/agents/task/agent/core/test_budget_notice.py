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


# 095 W1d: the one dollar-budget notice at 80 % of the per-run cap.
import pytest
from unittest.mock import AsyncMock

from agents.task.agent.core.budget_notice import maybe_push_dollar_notice
from agents.task.agent.core.run_budget import run_budget_fraction


def test_dollar_notice_fires_once_at_80_percent():
	agent = _agent(sub=True)
	agent._run_spend_seen = (1.5, 2.0)
	assert not maybe_push_dollar_notice(agent)
	assert agent.message_manager.pushed == []
	agent._run_spend_seen = (1.6, 2.0)
	assert maybe_push_dollar_notice(agent)
	agent._run_spend_seen = (1.9, 2.0)
	assert not maybe_push_dollar_notice(agent)
	assert len(agent.message_manager.pushed) == 1
	text = str(agent.message_manager.pushed[0].content)
	assert "This run has spent $1.60 of its $2.00 cost budget." in text
	assert "do not stop because of this notice" in text
	assert "one turn to report" in text  # a sub-agent gets the last report


def test_dollar_notice_has_its_own_flag():
	agent = _agent(sub=True)
	agent._budget_notice_sent = True  # the step notice already fired
	agent._run_spend_seen = (1.8, 2.0)
	assert maybe_push_dollar_notice(agent)


def test_no_per_run_cap_means_no_dollar_notice():
	agent = _agent(sub=True)
	assert not maybe_push_dollar_notice(agent)
	agent._run_spend_seen = (5.0, 0.0)
	assert not maybe_push_dollar_notice(agent)
	assert agent.message_manager.pushed == []


@pytest.mark.asyncio
async def test_the_gate_records_the_spend_for_the_notice(monkeypatch):
	from agents.task.agent.core.run_budget import check_run_budget
	monkeypatch.setattr("agents.task.agent.core.run_budget.run_budget_usd", lambda: 0.0)
	monkeypatch.setattr("agents.task.agent.core.run_budget.autonomous_run_budget_usd", lambda: 2.0)
	monkeypatch.setattr("agents.task.agent.core.run_budget._session_spend",
						AsyncMock(return_value=4.7))
	agent = SimpleNamespace(_is_sub_agent=False, state=SimpleNamespace(run_spend_baseline=3.0))
	assert await check_run_budget(agent) is None
	assert agent._run_spend_seen == pytest.approx((1.7, 2.0))


@pytest.mark.asyncio
async def test_run_budget_fraction_reads_the_delta_against_the_cap(monkeypatch):
	monkeypatch.setattr("agents.task.agent.core.run_budget.autonomous_run_budget_usd", lambda: 2.0)
	monkeypatch.setattr("agents.task.agent.core.run_budget._session_spend",
						AsyncMock(return_value=4.5))
	agent = SimpleNamespace(_is_sub_agent=False, state=SimpleNamespace(run_spend_baseline=3.0))
	assert await run_budget_fraction(agent) == pytest.approx(0.75)
	agent.state.run_spend_baseline = None  # an interactive run: no per-run cap
	assert await run_budget_fraction(agent) is None
	child = SimpleNamespace(_is_sub_agent=True, _run_budget_limit=(4.0, 1.0, "background"))
	assert await run_budget_fraction(child) == pytest.approx(0.5)
