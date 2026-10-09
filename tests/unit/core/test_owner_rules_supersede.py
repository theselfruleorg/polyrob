"""060 WS-6 — supersede, never evict.

2026-09-20: recording one owner rule took three attempts against the 1600-char
cap (1695/1600, 1612/1600) and only fit after text was deleted from the 09-17
and 09-18 rules. Now a line an update drops moves under ``## Superseded``, dated
(with its successor when one line replaced one line), the loader injects only
the ACTIVE part, and the cap counts only the active part.
"""

import pytest

from core.doc_claims import (SUPERSEDED_HEADING, SUPERSEDED_MAX_CHARS,
                             active_rule_lines, carry_superseded,
                             split_superseded, stamp_changed_lines,
                             superseded_entries)
from core.instance import OWNER_DOC_MAX_CHARS, load_owner_doc
from core.owner_doc_writer import OwnerDocWriter
from core.self_evolution import summarize_doc_change

OLD = "# Owner rules\n\n- No public bug-fix reports.\n- Post daily at 15:30.\n"


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
	monkeypatch.delenv("OWNER_RULES_SUPERSEDE", raising=False)


def test_a_replaced_line_is_superseded_with_date_and_successor():
	new = OLD.replace("15:30", "16:00")
	out = carry_superseded(OLD, new, observed_at="2026-09-23")
	active, sup = split_superseded(out)
	assert "16:00" in active and "15:30" not in active
	assert sup.strip() == "- Post daily at 15:30. — superseded 2026-09-23 by: Post daily at 16:00."


def test_a_deleted_line_is_kept_without_a_successor():
	new = "# Owner rules\n\n- Post daily at 15:30.\n"
	out = carry_superseded(OLD, new, observed_at="2026-09-23")
	assert superseded_entries(out) == ["- No public bug-fix reports. — superseded 2026-09-23"]


def test_the_section_cannot_be_erased_by_omitting_it():
	once = carry_superseded(OLD, OLD.replace("15:30", "16:00"), observed_at="2026-09-23")
	active, _sup = split_superseded(once)
	again = carry_superseded(once, active + "\n", observed_at="2026-09-24")
	assert superseded_entries(again) == superseded_entries(once)


def test_nothing_dropped_is_a_no_op():
	assert carry_superseded(OLD, OLD) == OLD
	assert carry_superseded("", OLD) == OLD


def test_headings_are_structure_not_rules():
	out = carry_superseded(OLD, "- No public bug-fix reports.\n- Post daily at 15:30.\n")
	assert superseded_entries(out) == []


def test_the_section_is_bounded_oldest_first():
	entries = "\n".join(f"- rule {i} " + "x" * 90 + " — superseded 2026-01-01" for i in range(SUPERSEDED_MAX_CHARS // 100 + 50))
	doc = "- live rule\n\n" + SUPERSEDED_HEADING + "\n\n" + entries + "\n"
	out = carry_superseded(doc, "- live rule\n")
	sup = split_superseded(out)[1]
	assert len(sup) <= SUPERSEDED_MAX_CHARS
	assert f"rule {SUPERSEDED_MAX_CHARS // 100 + 49} " in sup and "rule 0 " not in sup


def test_stamping_leaves_the_superseded_section_alone():
	doc = "- live rule\n\n" + SUPERSEDED_HEADING + "\n\n- old — superseded 2026-09-01\n"
	stamped = stamp_changed_lines("", doc, "owner said", "2026-09-23")
	assert stamped.splitlines()[0].endswith("[from: owner said 2026-09-23]")
	assert split_superseded(stamped)[1] == split_superseded(doc)[1]


def test_writer_supersedes_and_loader_injects_only_active(tmp_path):
	w = OwnerDocWriter(tmp_path)
	assert w.propose(OLD, user_id="u1", created_by="user", pending=False).ok
	res = w.propose(OLD.replace("15:30", "16:00"), user_id="u1", created_by="user",
	                pending=False, observed_at="2026-09-23")
	assert res.ok, res.errors
	raw = w.read("u1")
	assert SUPERSEDED_HEADING in raw and "15:30" in raw
	loaded = load_owner_doc(tmp_path, "u1")
	assert "16:00" in loaded and "15:30" not in loaded and "Superseded" not in loaded


def test_cap_counts_only_the_active_rules(tmp_path):
	w = OwnerDocWriter(tmp_path)
	big = "- " + "a" * (OWNER_DOC_MAX_CHARS - 20) + "\n"
	assert w.propose(big, user_id="u1", created_by="user", pending=False).ok
	# Replacing the big rule leaves it (superseded) in the file: the file is now
	# over the cap, the ACTIVE part is not — the write lands and the loader reads.
	res = w.propose("- a short rule\n", user_id="u1", created_by="user", pending=False)
	assert res.ok, res.errors
	assert len(w.read("u1")) > OWNER_DOC_MAX_CHARS
	assert load_owner_doc(tmp_path, "u1") == "- a short rule"


def test_flag_off_restores_the_flat_doc(tmp_path, monkeypatch):
	monkeypatch.setenv("OWNER_RULES_SUPERSEDE", "false")
	w = OwnerDocWriter(tmp_path)
	w.propose(OLD, user_id="u1", created_by="user", pending=False)
	w.propose(OLD.replace("15:30", "16:00"), user_id="u1", created_by="user", pending=False)
	assert SUPERSEDED_HEADING not in w.read("u1")


def test_forged_author_is_still_quarantined(tmp_path):
	"""WS-6 changes the body, never the gate."""
	w = OwnerDocWriter(tmp_path)
	w.propose(OLD, user_id="u1", created_by="user", pending=False)
	res = w.propose(OLD.replace("15:30", "16:00"), user_id="u1",
	                created_by="background_review", pending=False)
	assert res.ok and res.pending
	assert "15:30" in load_owner_doc(tmp_path, "u1")


def test_change_summary_reports_superseded_not_deleted():
	after = carry_superseded(OLD, OLD.replace("15:30", "16:00"), observed_at="2026-09-23")
	summary = summarize_doc_change(OLD, after)
	assert summary.startswith("+1/-1 lines")
	assert "1 superseded" in summary


def test_active_rule_lines():
	assert active_rule_lines(OLD) == ["- No public bug-fix reports.", "- Post daily at 15:30."]
