"""060 WS-4 — `owner_doc_manage(action='sweep')`, and the sweep that follows a
rule the owner puts in effect: it NAMES the standing instruction the new rule
overrides (advisory; nothing is edited)."""
import logging
import sqlite3
import types

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from tools.controller.registry.service import Registry
from tools.controller.service import Controller


def _controller(data_dir):
	c = object.__new__(Controller)
	c.logger = logging.getLogger("owner-doc-sweep-test")
	c.registry = Registry()
	c.user_id = "alice"
	c.session_id = "s1"
	c.container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(data_dir)))
	c._register_owner_doc_manage_action()
	return c.registry.registry.actions["owner_doc_manage"]


def _ctx():
	return types.SimpleNamespace(user_id="alice", is_sub_agent=False, role="orchestrator", metadata={})


def _cron(tmp_path, task):
	con = sqlite3.connect(tmp_path / "cron.db")
	con.execute("CREATE TABLE cron_jobs (id TEXT, task TEXT, user_id TEXT, enabled INTEGER)")
	con.execute("INSERT INTO cron_jobs VALUES ('dailyx01', ?, 'alice', 1)", (task,))
	con.commit(); con.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
	monkeypatch.setenv("OWNER_DOC_WRITABLE", "true")
	monkeypatch.delenv("POLYROB_LOCAL", raising=False)


@pytest.mark.asyncio
async def test_sweep_action_names_the_contradicting_cron_line(tmp_path):
	_cron(tmp_path, "Every day publish a public report of the bugs we fix.")
	a = _controller(tmp_path)
	res = await a.function(a.param_model(action="sweep",
	                                     content="Never publish public reports about bugs we fix."),
	                       execution_context=_ctx())
	assert "may contradict it" in res.extracted_content
	assert "cron dailyx01" in res.extracted_content


@pytest.mark.asyncio
async def test_sweep_requires_content(tmp_path):
	a = _controller(tmp_path)
	res = await a.function(a.param_model(action="sweep"), execution_context=_ctx())
	assert res.error and "content" in res.error


@pytest.mark.asyncio
async def test_model_rule_stays_pending_and_its_draft_is_swept(tmp_path):
	"""095 W10 (X-21): the draft is swept — the reply names the job prose the new
	rule would override — and stays pending until the owner reviews it."""
	_cron(tmp_path, "Every day publish a public report of the bugs we fix.")
	a = _controller(tmp_path)
	res = await a.function(a.param_model(reason="test reason", action="update",
	                                     content="- Never publish public reports about bugs we fix."),
	                       execution_context=_ctx())
	assert "NOT YET IN EFFECT" in res.extracted_content
	assert "cron dailyx01" in res.extracted_content
	assert "Advisory only" in res.extracted_content


@pytest.mark.asyncio
async def test_sweep_all_needs_no_content_and_writes_nothing(tmp_path):
	_cron(tmp_path, "Every day publish a public report of the bugs we fix.")
	a = _controller(tmp_path)
	from core.owner_doc_writer import OwnerDocWriter, PROVENANCE_USER
	writer = OwnerDocWriter(tmp_path)
	writer.propose("- Never publish public reports about bugs we fix.\n", user_id="alice",
	               created_by=PROVENANCE_USER, pending=False)
	before = writer.read_active("alice")
	res = await a.function(a.param_model(action="sweep_all"), execution_context=_ctx())
	assert "Never publish public reports about bugs we fix." in res.extracted_content
	assert "Every day publish a public report of the bugs we fix." in res.extracted_content
	assert writer.read_active("alice") == before
	assert writer.list_pending("alice") is None


@pytest.mark.asyncio
async def test_each_tool_path_refuses_job_state_and_names_the_verb(tmp_path):
	from core.owner_doc_writer import OwnerDocWriter, PROVENANCE_USER
	writer = OwnerDocWriter(tmp_path)
	writer.propose("- Never follow anyone.\n", user_id="alice", created_by=PROVENANCE_USER, pending=False)
	a = _controller(tmp_path)
	for params in (
		dict(action="update", content="- Never follow anyone.\n- The promo job is OFF.\n"),
		dict(action="patch", old_string="Never follow anyone.", new_string="Job a1b2c3d4e5f6 follows."),
		dict(action="replace_rule", rule_start="Never follow", new_string="The promo job is paused."),
	):
		res = await a.function(a.param_model(reason="test reason", **params), execution_context=_ctx())
		assert res.error and res.error.startswith("Nothing was written.")
		assert "cronjob_show" in res.error
		assert "Near the cap" not in res.error and "replace_rule" not in res.error
	assert writer.list_pending("alice") is None


def test_the_description_says_rules_only(tmp_path):
	a = _controller(tmp_path)
	assert "never here" in a.description and "cronjob_" in a.description
