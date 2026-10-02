"""060 WS-4 — "what contradicts this rule?" returns an answer instead of a grep.

The 09-21 shape: an owner rule forbade public bug-fix reports; a cron job's
prose asked for one, and the prose was the turn. The sweep names that line.
Records are never swept (WS-3), and the sweep never edits anything.
"""
import sqlite3

import pytest

from core.owner_doc_writer import OwnerDocWriter
from core.rules_sweep import rail_instruction_lines, render_sweep, sweep

UID = "u1"
RULE = "NO PUBLIC BUG-FIX REPORTS: never publish reports about bugs we fix."


@pytest.fixture(autouse=True)
def _env(monkeypatch):
	monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")


def _dbs(tmp_path, cron_tasks=(), goals=()):
	cron = tmp_path / "cron.db"
	con = sqlite3.connect(cron)
	con.execute("CREATE TABLE cron_jobs (id TEXT, task TEXT, user_id TEXT, enabled INTEGER)")
	for i, t in enumerate(cron_tasks):
		con.execute("INSERT INTO cron_jobs VALUES (?,?,?,1)", (f"job{i}aaaa", t, UID))
	con.commit(); con.close()
	gdb = tmp_path / "goals.db"
	con = sqlite3.connect(gdb)
	con.execute("CREATE TABLE goals (id TEXT, user_id TEXT, title TEXT, body TEXT, "
	            "status TEXT, payload TEXT)")
	for i, (title, status) in enumerate(goals):
		con.execute("INSERT INTO goals VALUES (?,?,?,?,?,?)", (f"g{i}", UID, title, "", status, "{}"))
	con.commit(); con.close()
	return str(cron), str(gdb)


def _run(tmp_path, rule=RULE, skills=(), **kw):
	cron, gdb = _dbs(tmp_path, **kw)
	return sweep(rule, user_id=UID, data_dir=str(tmp_path), instance_id="rob",
	             cron_db=cron, goals_db=gdb, skills=list(skills))


def test_cron_prose_that_contradicts_the_rule_is_named(tmp_path):
	res = _run(tmp_path, cron_tasks=["Daily: publish a public report on the bugs we fix today."])
	assert [h.surface for h in res["hits"]] == ["rail_prose"]
	assert res["hits"][0].where.startswith("cron job0")


def test_agreeing_lines_are_not_hits(tmp_path):
	res = _run(tmp_path, cron_tasks=["Never publish public bug reports."],
	           skills=[("x-engagement", "Do not post bug-fix reports publicly.")])
	assert res["hits"] == []


def test_skills_goals_and_owner_rules_are_swept(tmp_path):
	w = OwnerDocWriter(tmp_path, instance_id="rob")
	w.propose("- Publish every bug fix report on X.\n", user_id=UID, created_by="user", pending=False)
	res = _run(tmp_path, skills=[("rh-reporting", "Publish public reports of each bug fix.")],
	           goals=[("Publish the public bug report thread", "ready"),
	                  ("publish public bug report (done)", "done")])
	surfaces = sorted({h.surface for h in res["hits"]})
	assert surfaces == ["owner_rules", "rail_prose", "skills"]
	assert not any("(done)" in h.line for h in res["hits"])  # a finished goal no longer steers


def test_records_are_never_swept_instruction_docs_are(tmp_path):
	ws = tmp_path / "workspace" / "reports"; ws.mkdir(parents=True)
	(ws / "queue.md").write_text("- 2026-09-20 published a public report on the bug fix\n")
	(ws / "rules.md").write_text("---\nkind: instruction\n---\n- Publish a public bug fix report weekly.\n")
	res = _run(tmp_path)
	assert [h.where for h in res["hits"]] == ["reports/rules.md"]
	assert any("record(s) not swept" in n for n in res["notes"])


def test_render_is_advisory_and_names_the_precedence(tmp_path):
	res = _run(tmp_path, cron_tasks=["Publish a public report on each bug we fix."])
	text = render_sweep(res, RULE)
	assert "may contradict it" in text and "Advisory only" in text
	assert "owner rule" in text and "system prompt: code" in text
	clean = render_sweep(_run(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path), RULE)
	assert "nothing in the standing instructions contradicts it" in clean


def test_unprovided_skills_are_named_not_hidden(tmp_path):
	cron, gdb = _dbs(tmp_path)
	res = sweep(RULE, user_id=UID, data_dir=str(tmp_path), instance_id="rob",
	            cron_db=cron, goals_db=gdb, skills=None)
	assert "skills" not in res["swept"]
	assert any("skills: not provided" in n for n in res["notes"])


def test_rail_instruction_lines():
	lines = rail_instruction_lines("post daily\nat 15:30", {"skills": ["x-engagement"]})
	assert "later owner rule outranks it" in lines[0]
	assert lines[1:3] == ["  post daily", "  at 15:30"]
	assert lines[-1] == "pinned skills: x-engagement"


def test_active_rail_leg_is_swept_before_a_child_goal_exists(tmp_path):
    import json
    cron, goals = _dbs(tmp_path)
    con = sqlite3.connect(goals)
    payload = {"recurrence": {"legs": [{"title": "post", "body": "Publish public bug fix reports."}]}}
    con.execute("INSERT INTO goals VALUES (?,?,?,?,?,?)",
                ("rail", UID, "standing rail", "", "active", json.dumps(payload)))
    con.execute("INSERT INTO goals VALUES (?,?,?,?,?,?)",
                ("paused", UID, "off rail", "", "paused", json.dumps(payload)))
    con.commit(); con.close()
    result = sweep(RULE, user_id=UID, data_dir=str(tmp_path), instance_id="rob",
                   cron_db=cron, goals_db=goals, skills=[])
    assert len(result["hits"]) == 1
    assert result["hits"][0].where == "goal rail"
    assert "public bug fix" in result["hits"][0].line
