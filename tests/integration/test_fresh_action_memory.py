"""103 acceptance: the agent keeps what it did across many compactions at the output cap.

The shape of prod 2026-11-04, with neutral fixtures: an owner chat, 20 sends by card with
quote refreshes, long file reads, and a model that re-emits the whole prior summary and is
cut at its output cap. After six compactions the next prompt must hold the exact done count
and the newest tx hash, and a stored summary must never be a silently cut one.
"""
import re
import time

import pytest

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from core import session_ledger as L
from core.surfaces import cards
from modules.llm.messages import AIMessage, HumanMessage, MessageOrigin, ToolMessage

OWNER = "owner-1"
_CAP_CHARS = 22_000   # about 8,192 tokens of summary


class _GreedyModel:
	"""Re-emits the prior summary and adds to it, as "drop nothing" asked, up to the cap."""
	model_name = "gpt-5"

	def __init__(self):
		self.calls = 0

	async def ainvoke(self, messages, tools=None, **kwargs):
		self.calls += 1
		prompt = str(messages[-1].content)
		match = re.search(r"<prior_summary_data>\n(.*?)\n</prior_summary_data>", prompt, re.S)
		prior = match.group(1) if match else ""
		done = "\n".join(f"- paid winner {i} of this span with a long line of detail" * 3
		                 for i in range(60))
		text = (f"## Active Task\nthe task\n## Goal & Constraints\nthe goal\n"
		        f"## Completed Actions (with outcomes)\n{done}\n{prior}\n"
		        "## In Progress\n(none)\n## Blocked / Open Questions\n(none)\n"
		        "## Key Decisions\n(none)\n## Resolved Questions\n(none)\n"
		        "## Pending User Asks\nthe pending ask\n"
		        "## Relevant Files & Data\n- data/rounds/the-right-file.jsonl\n"
		        "## Remaining Work\nthe remaining work\n## End of summary")
		return AIMessage(content=text[:_CAP_CHARS])


@pytest.fixture
def store(tmp_path, monkeypatch):
	monkeypatch.setenv("TASK_MAX_MESSAGES", "800")
	monkeypatch.setenv("COMPACTION_CHECKPOINT", "false")
	monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "false")
	st = cards.CardStore(str(tmp_path / "cards.db"))
	cards.set_store(st)
	yield st
	cards.set_store(None)


def _send(st, n: int, *, refreshes: int = 2):
	"""One winner's payment: quote refreshes that were replaced, then the send."""
	parent = None
	for _ in range(refreshes):
		parent = _card(st, "replaced", "", parent)
	return _card(st, "done", f"Sent. tx 0x{n:064x}", parent)


def _card(st, state, result, parent):
	card = st.insert(cards.Card(
		card_id=cards.new_card_id(), user_id=OWNER, kind=cards.KIND_QUOTE, origin="agent",
		title="send", verb="/send", args=["311628", "TOK", "to", "0xabc"],
		parent_id=parent.card_id if parent else None, result=result,
		created_at=time.time(), updated_at=time.time(), expires_at=time.time() + 3600))
	conn = st._conn()
	try:
		conn.execute("UPDATE cards SET state=? WHERE card_id=?", (state, card.card_id))
		conn.commit()
	finally:
		conn.close()
	return card


def _manager(llm) -> MessageManager:
	mm = MessageManager(llm=llm, task="the chat", action_descriptions="actions",
	                    system_prompt_class=SystemPrompt, max_input_tokens=200_000,
	                    image_tokens=800, session_id="s-accept")
	mm.action_ledger_source = lambda: L.render(L.read_ledger(OWNER))
	mm._history_budget_tokens = 8_000   # the gauge stays over the band after ageing
	return mm


def _work(mm, turns: int = 60):
	"""Long reads and writes: the history that pushes a chat over its budget."""
	for i in range(turns):
		call_id = f"c{time.time_ns()}"
		mm._add_message_with_tokens(
			AIMessage(content="", tool_calls=[{"id": call_id, "name": "write_file", "type": "function",
			                                   "function": {"name": "write_file", "arguments": "{}"},
			                                   "args": {"content": "w" * 2000}}]), _internal=True)
		mm._add_message_with_tokens(ToolMessage(content="r" * 4000, tool_call_id=call_id), _internal=True)


def _texts(mm):
	return [str(m.message.content) for m in mm.history.messages]


@pytest.mark.asyncio
async def test_six_compactions_at_the_cap_keep_the_exact_count_and_the_newest_payment(store):
	llm = _GreedyModel()
	mm = _manager(llm)
	mm._add_message_with_tokens(HumanMessage(content="pay the 20 winners"), _internal=True)
	paid = 0
	for _ in range(6):
		for _ in range(3):
			paid += 1
			_send(store, paid)
		_work(mm)
		assert await mm.llm_compact_history()
		report = mm.last_summary_report
		summary = next(t for t in _texts(mm) if "[COMPACTED SESSION HISTORY]" in t)
		# Never silently cut: complete, or a salvage that says so.
		assert report["complete"] or "## Compaction note" in summary
		assert report["summary_chars"] > 0 and report["tail_tokens"] > 0
		assert len(summary) < 45_000, "the summary cannot grow compaction after compaction"

	ledgers = [str(m.message.content) for m in mm.history.messages
	           if getattr(m.message, "origin", None) == MessageOrigin.ACTION_LEDGER]
	assert len(ledgers) == 1, "one block, re-read from the stores at each rebuild"
	assert f"/send done {paid}" in ledgers[0]
	assert "no quote" not in ledgers[0].split("\n")[2], "a replaced quote is not a payment"
	assert f"0x{paid:064x}" in ledgers[0], "the newest payment is on the first screen"
	summary = next(t for t in _texts(mm) if "[COMPACTED SESSION HISTORY]" in t)
	assert "the-right-file.jsonl" in summary, "a section past the cut comes back from the prior"


@pytest.mark.asyncio
async def test_a_whole_answer_is_not_retried(store):
	class _Short(_GreedyModel):
		async def ainvoke(self, messages, tools=None, **kwargs):
			self.calls += 1
			return AIMessage(content="## Active Task\nx\n## End of summary")
	llm = _Short()
	mm = _manager(llm)
	mm._add_message_with_tokens(HumanMessage(content="go"), _internal=True)
	_work(mm)
	assert await mm.llm_compact_history()
	assert llm.calls == 1
	report = mm.last_summary_report
	assert (report["complete"], report["retried"]) == (True, False)
	assert report["summary_chars"] == len("## Active Task\nx") and report["tail_tokens"] > 0


def test_a_standing_job_reports_why_it_stopped_and_the_reader_sees_it(tmp_path):
	"""The job of prod 2026-11-04: its record said it stopped, the row said done."""
	from datetime import datetime, timezone
	from types import SimpleNamespace
	from cron.jobs import CronJob, CronJobStore
	from cron.runner import declared_outcome
	from cron.service import last_outcome

	text = "The round is blocked: no approved line for it. Owner notified."
	run = SimpleNamespace(blocked=False, outcome_line=None, done_text=text)
	assert declared_outcome(run) == ("blocked", text)
	assert declared_outcome(SimpleNamespace(blocked=False, outcome_line=None,
	                                        done_text="Posted. No longer blocked."))[0] == "done"
	store = CronJobStore(str(tmp_path / "cron.db"))
	job = store.add(CronJob(id="job-1", user_id=OWNER, task="post the round", schedule_spec="every 3d", next_run_at=None))
	store.record_outcome(job.id, "blocked", datetime.now(timezone.utc), reason=text, report=text)
	row = store.get(job.id)
	assert last_outcome(row).startswith("blocked")
	assert row.payload["last_outcome"]["report"] == text


def test_a_salvaged_summary_shows_on_status_and_a_whole_one_clears_it(store, tmp_path, monkeypatch):
	from core import credential_verdicts as CV
	from core.status_rails import _rail_verdict_line
	monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
	mm = _manager(_GreedyModel())
	mm._summary_done("x", retried=True, complete=False)
	verdict = CV.verdict("compaction_summary")
	assert verdict is not None
	line, _, key = _rail_verdict_line(verdict)
	assert "summary was cut" in line and "/new" in line and key == "compaction_summary_cut"
	mm._summary_done("y", retried=False)
	assert CV.verdict("compaction_summary") is None
