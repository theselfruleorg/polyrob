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
	c.user_id = "rob"
	c.session_id = "s1"
	c.container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(data_dir)))
	c._register_owner_doc_manage_action()
	return c.registry.registry.actions["owner_doc_manage"]


def _ctx():
	return types.SimpleNamespace(user_id="rob", is_sub_agent=False, role="orchestrator", metadata={})


def _cron(tmp_path, task):
	con = sqlite3.connect(tmp_path / "cron.db")
	con.execute("CREATE TABLE cron_jobs (id TEXT, task TEXT, user_id TEXT, enabled INTEGER)")
	con.execute("INSERT INTO cron_jobs VALUES ('dailyx01', ?, 'rob', 1)", (task,))
	con.commit(); con.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
	monkeypatch.setenv("OWNER_DOC_WRITABLE", "true")
	monkeypatch.setenv("OWNER_RULES_IMMEDIATE", "true")
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
async def test_model_rule_stays_pending_until_owner_review(tmp_path):
	_cron(tmp_path, "Every day publish a public report of the bugs we fix.")
	a = _controller(tmp_path)
	res = await a.function(a.param_model(action="update",
	                                     content="- Never publish public reports about bugs we fix."),
	                       execution_context=_ctx())
	assert "NOT YET IN EFFECT" in res.extracted_content
	assert "cron dailyx01" not in res.extracted_content
