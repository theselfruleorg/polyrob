"""A job's own row is the record of its state: owner.md holds standing rules only.

The check is one regex-based function on every path that writes the doc; the frozen
``## History`` section shares the ``## Superseded`` parser; the conflict check reuses
the sweep's own test and finds a rule that a job text contradicts.
"""
import sqlite3

import pytest

from core.doc_claims import (HISTORY_HEADING, SUPERSEDED_HEADING, active_rule_lines,
                             carry_superseded, changed_lines, history_text, job_state_claim,
                             rebase_draft, split_superseded, split_superseded_raw,
                             stamp_changed_lines, superseded_entries)
from core.instance import DEFAULT_INSTANCE_ID, load_owner_doc
from core.owner_doc_writer import OwnerDocWriter
from core.self_context_writer import PROVENANCE_USER
from core.rules_sweep import conflict_check, render_conflict_check, sweep
from cron.owner_rules_overlay import newer_owner_rules

JOB_ID = "a1b2c3d4e5f6"


@pytest.mark.parametrize("line", [
    f"- The promo job is {JOB_ID}.",
    f"Run `{JOB_ID}` every day",
    f"job_id={JOB_ID}",
    f"- Reply lane ({JOB_ID}) answers mentions",
    "- The promo job is OFF.",
    "- the promo job is off",
    "- The promo job is **OFF** until the owner says so",
    "- Cron job is now paused.",
    "- Promo job paused until Monday",
    "- Reply lane: disabled",
    "- the follow job OFF",
    "- The buyback is OFF.",
    "- Buyback: OFF",
    "- BUYBACKS STOPPED",
    "- BUYBACKS STOPPED for good.",
    "- BUYBACKS STOPPED (2026-01-02): the owner said stop.",
    "- Do not retry: the promo job is off.",
    "- The bridge job is on hold.",
])
def test_a_job_state_line_is_found(line):
    assert job_state_claim(line)


@pytest.mark.parametrize("line", [
    "- NEVER follow anyone.",
    "- Always ask before paying.",
    "- Turn the sound OFF after 22:00.",
    "- TURN OFF the alerts at night.",
    "- Back off when a site says no.",
    "- Run the cron job on Friday.",
    "- The job is on Monday.",
    "- The meeting is on Monday.",
    "- The wallet is on the main chain.",
    "- Stop posting on X.",
    "- Do not turn off notifications.",
    "- Never restart a job that is paused.",
    "- When a job is paused, tell the owner.",
    "- If the cron is off, say so.",
    "- A job is stopped by a pause.",
    "- Keep each job text short.",
    "- Contact number 123456789012.",
    "- Pay to 0xabc123def456 only.",
    "- See https://example.com/p/abc123def456 for the terms.",
    "- Trace id 123e4567-e89b-12d3-a456-426614174000 is a sample.",
    "- Files live in /srv/abc123def456/data.",
    "Timezone: UTC",
    "## Standing rules",
])
def test_a_real_rule_is_not_refused(line):
    assert job_state_claim(line) == ""


def test_the_stamp_is_not_read():
    assert job_state_claim(f"- Never post at night. [from: job {JOB_ID} 2026-01-02]") == ""


@pytest.fixture
def writer(tmp_path):
    return OwnerDocWriter(tmp_path)


def _active(writer, body):
    res = writer.propose(body, user_id="alice", created_by=PROVENANCE_USER, pending=False)
    assert res.ok, res.errors
    return res


def test_update_is_refused_with_the_reason_and_the_verb(writer):
    res = writer.propose("- The promo job is OFF.\n", user_id="alice",
                         created_by=PROVENANCE_USER, pending=False)
    assert not res.ok
    assert "standing rules only" in res.errors[0] and "cronjob_show" in res.errors[0]
    assert writer.read_active("alice") == ""


def test_a_draft_is_refused_when_written(writer):
    res = writer.propose(f"- Run {JOB_ID}.\n", user_id="alice")
    assert not res.ok and writer.list_pending("alice") is None


def test_patch_is_refused(writer):
    _active(writer, "- Never follow anyone.\n")
    res = writer.patch(user_id="alice", old_string="Never follow anyone.",
                       new_string=f"Never follow anyone; job {JOB_ID} handles replies.",
                       created_by=PROVENANCE_USER, pending=False)
    assert not res.ok and "job's state" in res.errors[0]
    assert "handles replies" not in writer.read_active("alice")


def test_promote_refuses_a_draft_queued_before_the_check(writer):
    _active(writer, "- Never follow anyone.\n")
    pending = writer._pending_file("alice")
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("- Never follow anyone.\n- The promo job is OFF.\n", encoding="utf-8")
    res = writer.promote(user_id="alice")
    assert not res.ok and "job's state" in res.errors[0]
    assert "promo job" not in writer.read_active("alice")


def _legacy(writer, body):
    path = writer._active_file("alice")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_an_older_line_does_not_block_another_edit_and_can_be_retired(writer):
    _legacy(writer, f"- Old line about {JOB_ID}.\n- Never follow anyone.\n")
    res = writer.patch(user_id="alice", old_string="Never follow anyone.",
                       new_string="NEVER follow anyone.", created_by=PROVENANCE_USER, pending=False)
    assert res.ok, res.errors
    res = writer.patch(user_id="alice", old_string=f"- Old line about {JOB_ID}.\n", new_string="",
                       created_by=PROVENANCE_USER, pending=False)
    assert res.ok, res.errors
    assert JOB_ID in split_superseded(writer.read_active("alice"))[1]
    assert JOB_ID not in split_superseded(writer.read_active("alice"))[0]


def test_a_retired_state_line_cannot_return_to_the_active_part(writer):
    _legacy(writer, f"- Keep posting.\n\n{SUPERSEDED_HEADING}\n\n- The promo job is OFF.\n")
    res = writer.patch(user_id="alice", old_string="Keep posting.", new_string="The promo job is OFF.",
                       created_by=PROVENANCE_USER, pending=False)
    assert not res.ok


@pytest.mark.parametrize("heading", [SUPERSEDED_HEADING, HISTORY_HEADING])
def test_a_frozen_section_is_not_checked(writer, heading):
    res = _active(writer, f"- Never follow anyone.\n\n{heading}\n\n- Job {JOB_ID} is OFF.\n")
    assert JOB_ID in writer.read_active("alice") and res.ok


def test_the_self_doc_has_no_such_check(tmp_path):
    from core.self_context_writer import SelfContextWriter
    res = SelfContextWriter(tmp_path).propose("- The promo job is OFF.\n", user_id="alice")
    assert res.ok


# --- ## History -------------------------------------------------------------------

DOC = ("# Rules\n\n- Never follow anyone.\n\n"
       f"{HISTORY_HEADING}\n\n2026-01-02: we moved the report to Friday.\n\n"
       f"{SUPERSEDED_HEADING}\n\n- Old rule — superseded 2026-01-01\n")


def test_the_parser_reads_both_sections_in_either_order():
    active, sup = split_superseded(DOC)
    assert active == "# Rules\n\n- Never follow anyone."
    assert sup == "- Old rule — superseded 2026-01-01"
    assert history_text(DOC) == "2026-01-02: we moved the report to Friday."
    assert superseded_entries(DOC) == ["- Old rule — superseded 2026-01-01"]
    assert active_rule_lines(DOC) == ["- Never follow anyone."]


def test_history_only_doc():
    doc = f"- A rule.\n\n{HISTORY_HEADING}\n\n- A past note.\n"
    assert split_superseded(doc) == ("- A rule.", "")
    head, tail = split_superseded_raw(doc)
    assert head + tail == doc and tail.startswith(HISTORY_HEADING)


def test_the_section_is_not_a_change_and_is_never_stamped():
    assert changed_lines("- A rule.\n", f"- A rule.\n\n{HISTORY_HEADING}\n\n- A note.\n") == []
    out = stamp_changed_lines("", f"- A rule.\n\n{HISTORY_HEADING}\n\n- A note.\n", "owner said",
                              "2026-01-02")
    assert "- A note.\n" in out and "- A note. [from" not in out


def test_the_section_cannot_be_erased_and_only_grows():
    out = carry_superseded(DOC, "- Never follow anyone.\n", observed_at="2026-01-03")
    assert history_text(out) == history_text(DOC)
    assert "Old rule" in split_superseded(out)[1]
    rewritten = f"- Never follow anyone.\n\n{HISTORY_HEADING}\n\n- A new note.\n"
    out = carry_superseded(DOC, rewritten, observed_at="2026-01-03")
    assert history_text(out).splitlines() == [
        "2026-01-02: we moved the report to Friday.", "- A new note."]


def test_a_line_moved_to_history_is_not_also_superseded():
    old = "- A rule.\n- We ran a promo in March.\n"
    new = f"- A rule.\n\n{HISTORY_HEADING}\n\n- We ran a promo in March.\n"
    out = carry_superseded(old, new, observed_at="2026-01-03")
    assert superseded_entries(out) == []
    assert "promo" not in split_superseded(out)[0] and "promo" in history_text(out)


def test_carry_is_idempotent():
    once = carry_superseded(DOC, DOC, observed_at="2026-01-03")
    assert carry_superseded(once, once, observed_at="2026-01-03") == once


def test_rebase_keeps_history_in_the_tail():
    draft = f"- Rule one.\n- Rule two.\n\n{HISTORY_HEADING}\n\n- Note.\n"
    merged, _retired, _kept = rebase_draft("- Rule one.\n", draft, "- Rule one.\n- Added since.\n")
    assert history_text(merged) == "- Note." and "Added since" in split_superseded(merged)[0]


def test_loader_cap_and_patch_leave_history_out(writer, tmp_path):
    big = "x" * 30000
    _active(writer, f"- Never follow anyone.\n\n{HISTORY_HEADING}\n\n{big}\n")
    loaded = load_owner_doc(tmp_path, "alice")
    assert loaded == "- Never follow anyone."
    res = writer.patch(user_id="alice", old_string=big[:40], new_string="y",
                       created_by=PROVENANCE_USER, pending=False)
    assert not res.ok and "frozen" in res.errors[0]


def test_a_second_history_heading_is_refused(writer):
    res = writer.propose(f"- A.\n\n{HISTORY_HEADING}\n\n- B.\n\n{HISTORY_HEADING}\n\n- C.\n",
                         user_id="alice", created_by=PROVENANCE_USER, pending=False)
    assert not res.ok and HISTORY_HEADING in res.errors[0]


def test_a_history_heading_above_live_rules_is_refused_for_an_agent(writer):
    _active(writer, "- One.\n- Two.\n- Three.\n- Four.\n")
    res = writer.propose(f"{HISTORY_HEADING}\n\n- One.\n- Two.\n- Three.\n- Four.\n", user_id="alice")
    assert not res.ok and "retire" in res.errors[0]


def test_the_overlay_reads_active_lines_only():
    from datetime import datetime
    doc = (f"- Live. [from: owner said 2026-03-01]\n\n{HISTORY_HEADING}\n\n"
           "- Past. [from: owner said 2026-03-01]\n")
    assert newer_owner_rules(doc, datetime(2026, 1, 1)) == ["- Live."]


def test_status_counts_history_apart(tmp_path, writer):
    from core.status_rules import _owner_rules
    from core.status_snapshot import Section
    _active(writer, f"- Live rule.\n\n{HISTORY_HEADING}\n\n- Past one.\n- Past two.\n")
    sec = Section("rules")
    _owner_rules(sec, str(tmp_path), "alice", DEFAULT_INSTANCE_ID, {})
    assert sec.data["owner_rules"]["active"] == 1
    assert sec.data["owner_rules"]["history"] == 2
    assert "2 history line(s)" in sec.lines[0]


# --- the conflict check -----------------------------------------------------------


def _dbs(tmp_path, jobs):
    cron = tmp_path / "cron.db"
    con = sqlite3.connect(cron)
    con.execute("CREATE TABLE cron_jobs (id TEXT, task TEXT, user_id TEXT, enabled INTEGER, payload TEXT)")
    for jid, task, enabled in jobs:
        con.execute("INSERT INTO cron_jobs VALUES (?, ?, 'alice', ?, '{\"skills\": [\"lane-terms\"]}')",
                    (jid, task, enabled))
    con.commit(); con.close()
    return str(cron), str(tmp_path / "goals.db")


def _check(tmp_path, jobs, skills=None):
    cron, goals = _dbs(tmp_path, jobs)
    return conflict_check(user_id="alice", data_dir=str(tmp_path), instance_id=DEFAULT_INSTANCE_ID,
                          cron_db=cron, goals_db=goals, skills=skills)


def test_a_rule_a_job_contradicts_is_found_with_both_quotes(tmp_path, writer):
    _active(writer, "- Never publish public reports about bugs.\n- Always use the formal tone.\n")
    res = _check(tmp_path, [("job00001", "Every day publish a public report of the bugs we fix.", 1),
                            ("job00002", "Check the weather.", 1)])
    assert [h.where for h in res["hits"]] == ["cron job00001"]
    text = render_conflict_check(res)
    assert "Never publish public reports about bugs." in text
    assert "Every day publish a public report of the bugs we fix." in text
    assert "nothing was changed" in text


def test_a_disabled_job_is_not_read(tmp_path, writer):
    _active(writer, "- Never publish public reports about bugs.\n")
    res = _check(tmp_path, [("job00001", "Every day publish a public report of the bugs we fix.", 0)])
    assert res["hits"] == [] and res["counts"]["rails"] == 0


def test_a_pinned_skill_is_checked(tmp_path, writer):
    _active(writer, "- Never publish public reports about bugs.\n")
    res = _check(tmp_path, [], skills=[("lane-terms", "Publish a public report on each bugs list.")])
    assert [h.where for h in res["hits"]] == ["skill lane-terms"]


def test_an_older_state_line_is_found_beside_the_job_that_runs(tmp_path, writer):
    """The sweep's prohibition test has no word to see here; the state line check does."""
    _legacy(writer, "- BUYBACKS STOPPED\n- Never follow anyone.\n")
    res = _check(tmp_path, [("job00001", "Run the buyback of the token every 6 hours.", 1)])
    assert res["hits"] == []
    assert [s["rule"] for s in res["state_lines"]] == ["- BUYBACKS STOPPED"]
    text = render_conflict_check(res)
    assert "holds a job's state" in text
    assert 'cron job00001 says: "Run the buyback of the token every 6 hours."' in text


def test_the_plain_sweep_alone_misses_that_line(tmp_path, writer):
    cron, goals = _dbs(tmp_path, [("job00001", "Run the buyback of the token every 6 hours.", 1)])
    res = sweep("- BUYBACKS STOPPED", user_id="alice", data_dir=str(tmp_path), instance_id=DEFAULT_INSTANCE_ID,
                cron_db=cron, goals_db=goals, skills=[])
    assert res["hits"] == []


def test_an_unreadable_doc_is_not_a_clean_check(tmp_path, writer, monkeypatch):
    _active(writer, "- A rule.\n")
    monkeypatch.setattr("modules.memory.task.threat_scan.is_identity_suspicious", lambda _t: True)
    res = _check(tmp_path, [("job00001", "Publish the rule.", 1)])
    assert "could not read" in render_conflict_check(res)
    assert res["counts"]["rules"] == 0


def test_no_doc_and_no_job_says_it_read_nothing(tmp_path):
    res = _check(tmp_path, [])
    assert res["hits"] == [] and res["counts"] == {"rules": 0, "rails": 0, "skills": 0}
    assert res["notes"] == ["skills: not provided by the caller — not checked"]


# ── A `## History` the owner wrote before the frozen section existed ──────────────

def test_a_history_with_a_later_heading_keeps_the_rules_below_it_active():
    from core.doc_claims import active_rule_lines, history_text, split_superseded_raw
    doc = "## Rules\n- one\n## History\n- we met in May\n## Preferences\n- be brief\n"
    assert active_rule_lines(doc) == ["- one", "- we met in May", "- be brief"]
    assert history_text(doc) == ""
    assert split_superseded_raw(doc) == (doc, "")


def test_a_closing_history_stays_frozen_with_a_superseded_on_either_side():
    from core.doc_claims import active_rule_lines, history_text, split_superseded
    for doc in ("- one\n## History\n- old\n## Superseded\n- gone\n",
                "- one\n## Superseded\n- gone\n## History\n- old\n"):
        assert active_rule_lines(doc) == ["- one"]
        assert history_text(doc) == "- old"
        assert split_superseded(doc)[1] == "- gone"


def test_a_superseded_section_keeps_a_retired_heading_frozen():
    from core.doc_claims import active_rule_lines
    assert active_rule_lines("- one\n## Superseded\n## Old rules\n- gone\n") == ["- one"]
