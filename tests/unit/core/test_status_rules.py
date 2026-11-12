"""060 WS-7 — the `rules` status section: a rule that is NOT in effect says so.

The four 2026-09-21 defects all failed SILENT. This section reports, per
instruction surface, what is active / pending / superseded and names anything
written but not loaded as a health item (which also reaches <live-health>).
"""
import sqlite3

import pytest

from core.instance import OWNER_DOC_MAX_CHARS, self_tier_root
from core.owner_doc_writer import OwnerDocWriter
from core.status_rules import rules_section
from core.status_snapshot import STATE_OK

UID = "u1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
	monkeypatch.setenv("POLYROB_INSTANCE_ID", "alice")


def _keys(sec):
	return {h.key for h in sec.health}


def _cron(tmp_path, jobs):
	db = tmp_path / "cron.db"
	con = sqlite3.connect(db)
	con.execute("CREATE TABLE cron_jobs (id TEXT, task TEXT, user_id TEXT, payload TEXT, enabled INTEGER)")
	for jid, task, payload in jobs:
		con.execute("INSERT INTO cron_jobs VALUES (?,?,?,?,1)", (jid, task, UID, payload))
	con.commit(); con.close()
	return str(db)


def test_a_fresh_install_is_quiet(tmp_path):
	sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
	assert sec.health == [] and sec.state == STATE_OK
	blob = "\n".join(sec.lines)
	assert "owner rules: none written" in blob
	assert "rail prose: no cron store yet" in blob


def test_active_and_superseded_rules_are_counted(tmp_path):
	w = OwnerDocWriter(tmp_path, instance_id="alice")
	w.propose("- Rule A\n- Rule B\n", user_id=UID, created_by="user", pending=False)
	w.propose("- Rule A\n- Rule C\n", user_id=UID, created_by="user", pending=False)
	sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
	info = sec.data["owner_rules"]
	assert (info["active"], info["superseded"], info["loaded"]) == (2, 1, True)
	assert "2 active · 1 superseded" in sec.lines[0]


def test_a_pending_rule_is_named_as_not_in_effect(tmp_path):
	w = OwnerDocWriter(tmp_path, instance_id="alice")
	w.propose("- Never post bug-fix reports.\n", user_id=UID, created_by="agent", pending=True)
	sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
	assert "rule_pending" in _keys(sec)
	item = next(h for h in sec.health if h.key == "rule_pending")
	assert "NOT in effect" in item.text and "bug-fix" in item.text


def test_written_but_not_loaded_is_critical(tmp_path, monkeypatch):
	import modules.memory.task.threat_scan as ts
	monkeypatch.setattr(ts, "is_identity_suspicious", lambda _t: True)
	root = self_tier_root(tmp_path, UID, "alice")
	root.mkdir(parents=True, exist_ok=True)
	(root / "owner.md").write_text("- a rule the identity scan flags\n")
	sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
	assert "rules_owner_not_loaded" in _keys(sec)


def test_an_over_cap_owner_doc_is_named_as_truncated(tmp_path):
	"""2026-10-11: over the cap loads the head (it used to drop every rule)."""
	root = self_tier_root(tmp_path, UID, "alice")
	root.mkdir(parents=True, exist_ok=True)
	(root / "owner.md").write_text("- " + "x" * (OWNER_DOC_MAX_CHARS + 10))  # a direct-FS over-cap write
	sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
	assert "rules_owner_truncated" in _keys(sec)
	assert "rules_owner_not_loaded" not in _keys(sec)


def test_rail_prose_is_measured(tmp_path):
	db = _cron(tmp_path, [("daily-x", "post " * 200, '{"skills": ["x-engagement"]}'),
	                      ("digest", "summarize", "{}")])
	sec = rules_section(UID, str(tmp_path), db)
	info = sec.data["rail_prose"]
	assert info["jobs"] == 2 and info["pinned_skills"] == 1
	assert info["largest"]["id"] == "daily-x"
	assert any("1 pin their skills" in l for l in sec.lines)


def test_a_truncated_soul_doc_is_named(tmp_path):
	from core.instance import SELF_CONTEXT_PER_DOC_MAX_CHARS
	d = tmp_path / "identity"; d.mkdir()
	(d / "identity.md").write_text("I am Nova. " * (SELF_CONTEXT_PER_DOC_MAX_CHARS // 5))
	sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
	assert "rules_soul_truncated" in _keys(sec)


def test_every_surface_is_reported(tmp_path):
	from core.instruction_surfaces import SURFACE_NAMES
	sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
	assert set(sec.data["surfaces"]) == set(SURFACE_NAMES)


def test_the_snapshot_carries_the_section(tmp_path):
	from core.status_snapshot import SECTION_ORDER, build_status_snapshot
	assert "rules" in SECTION_ORDER
	snap = build_status_snapshot(UID, data_dir=str(tmp_path))
	assert snap.sections["rules"].available


def test_workspace_doctrine_counts_marked_docs(tmp_path, monkeypatch):
	monkeypatch.setenv("POLYROB_PROJECT_DIR", str(tmp_path / "workspace"))
	ws = tmp_path / "workspace" / "reports"; ws.mkdir(parents=True)
	(ws / "rules.md").write_text("---\nkind: instruction\n---\n- rule\n")
	(ws / "log.md").write_text("- 2026-09-20 posted\n")
	sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
	info = sec.data["workspace_doctrine"]
	assert (info["instruction"], info["undeclared"]) == (1, 1)
	assert any("1 instruction doc(s)" in l for l in sec.lines)


def test_goal_prose_is_reported_without_a_cron_store(tmp_path):
    con = sqlite3.connect(tmp_path / "goals.db")
    con.execute("CREATE TABLE goals (id TEXT,user_id TEXT,title TEXT,body TEXT,status TEXT,payload TEXT)")
    rows = [("g", UID, "goal", "Live task text", "ready", "{}"),
            ("old", UID, "history", "Ignored completed text", "done", "{}"),
            ("other", "different-user", "private", "Other tenant", "ready", "{}")]
    con.executemany("INSERT INTO goals VALUES (?,?,?,?,?,?)", rows)
    con.commit(); con.close()
    sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
    info = sec.data["rail_prose"]
    assert info["goal_rows"] == 1
    assert info["goal_chars"] == len("goal\nLive task text")
    assert any("1 live goal row(s)" in line for line in sec.lines)


def test_unreadable_goal_prose_never_renders_as_empty(tmp_path):
    (tmp_path / "goals.db").write_text("not sqlite")
    sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
    info = sec.data["rail_prose"]
    assert "unreadable" in info["goal_state"]
    assert "goal_rows" not in info


def test_skill_pending_count_does_not_claim_an_approval_or_loaded_inventory(tmp_path):
    sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
    info = sec.data["skills"]
    assert info["pending"] == 0
    # 116 W5.5: no fixed "unavailable" line; the knowledge section counts skills.
    assert set(info) == {"surface", "pending"}
    assert not any(l.startswith("skills:") for l in sec.lines)


def test_the_rules_are_read_from_the_identity_home_not_a_container_data_dir(tmp_path, monkeypatch):
    """116 W7.3: `agent_status` passes `<home>/data`; the rules are in `<home>`."""
    from core.status_rules import identity_home
    home = tmp_path / "home"
    (home / "identity").mkdir(parents=True)
    (home / "data").mkdir()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    assert identity_home(str(home / "data")) == str(home)
    assert identity_home(str(home)) == str(home)
    other = tmp_path / "other"
    other.mkdir()
    assert identity_home(str(other)) == str(other)      # not inside the home


def test_an_unreadable_draft_store_is_named(tmp_path, monkeypatch):
    from core import self_evolution

    def boom(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(self_evolution, "_writer", boom)
    sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
    assert any(l.startswith("pending drafts: unreadable (self-context: PermissionError")
               for l in sec.lines), sec.lines
    assert any(h.key == "rules_pending_unreadable" for h in sec.health)


def test_workspace_doctrine_reads_the_one_persistent_workspace(tmp_path, monkeypatch):
	"""116 W5.4: the reader reads the folder the agent works in, never
	<data_dir>/workspace (a path nothing creates)."""
	(tmp_path / "workspace").mkdir()
	(tmp_path / "workspace" / "rules.md").write_text("---\nkind: instruction\n---\n- rule\n")
	proj = tmp_path / "project"; proj.mkdir()
	monkeypatch.setenv("POLYROB_PROJECT_DIR", str(proj))
	info = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db")).data["workspace_doctrine"]
	assert info["instruction"] == 0 and info["root"] == str(proj.resolve())
	monkeypatch.delenv("POLYROB_PROJECT_DIR")
	monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
	sec = rules_section(UID, str(tmp_path), str(tmp_path / "cron.db"))
	assert "workspace doctrine: no persistent workspace (each session has its own)" in sec.lines


def test_persistent_workspace_root_is_the_bootstrap_rule(tmp_path, monkeypatch):
	from core.runtime_paths import persistent_workspace_root
	from polyrob.app.bootstrap import _resolve_cli_data_home
	monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
	monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
	monkeypatch.chdir(tmp_path)
	assert persistent_workspace_root() == str(tmp_path.resolve())
	monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "d"))
	assert persistent_workspace_root() is None
	assert _resolve_cli_data_home()[1:] == (False, None)
	monkeypatch.setenv("POLYROB_PROJECT_DIR", str(tmp_path / "p"))
	assert persistent_workspace_root() == str((tmp_path / "p").resolve())
	assert _resolve_cli_data_home()[1:] == (True, str((tmp_path / "p").resolve()))
