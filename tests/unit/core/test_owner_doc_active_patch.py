"""owner.md self-configuration defects (prod, 72h to 2026-10-11).

8 of Rob's ``owner_doc_manage`` patches failed:

- 3 with "old_string occurs 4× — pass replace_all=true": the count ran over the
  WHOLE file, and every extra copy sat in ``## Superseded``. Following the hint
  rewrote history and left the ACTIVE rule naming the old cron id.
- 5 at the cap ("16668/16000 chars"): the active rules were at 99.7% of it.

Also: Superseded held near-identical entries (the same retired rule with the
same successor, only the "superseded <date>" differed), and ``load_owner_doc``
dropped EVERY owner rule from the prompt when the active part was over the cap.
"""
import types

import pytest

from core.doc_claims import (SUPERSEDED_HEADING, carry_superseded,
                             split_superseded, superseded_entries)
from core.instance import OWNER_DOC_MAX_CHARS, load_owner_doc, self_tier_root
from core.owner_doc_writer import OwnerDocWriter

STOP_0909 = ("BUYBACKS STOPPED (2026-10-09 03:29): owner said \"Stop buybacks\". "
             "Cron 4def3260d811 CANCELLED. Supersedes BUYBACKS RESUMED (2026-10-07 05:45).")
RESUMED = ("- BUYBACKS RESUMED (2026-10-07 05:45): owner said \"start buybacks again\". "
           "New recurring job 4def3260d811.")


def _prod_shaped_doc() -> str:
	"""The prod shape: one active line names the phrase, Superseded names it 4x."""
	active = ("NO TOKEN SHILLING (2026-10-06): never shill the token.\n\n"
	          f"{STOP_0909}\n\n"
	          "LINK RULE (2026-09-18): polyrob.dev in all comms.\n")
	sup = "\n".join([
		f"{RESUMED} — superseded 2026-10-07 by: BUYBACKS RESUMED (2026-10-07 05:45) every 4h",
		f"{RESUMED} every 4h — superseded 2026-10-09 by: {STOP_0909[:60]}…",
		f"{RESUMED} every 6h — superseded 2026-10-10 by: {STOP_0909[:60]}…",
		"- PNL BUYBACK — FIXED SIZE 0.05 ETH (2026-09-23 17:01) — superseded 2026-10-05",
	])
	return active + "\n" + SUPERSEDED_HEADING + "\n\n" + sup + "\n"


def _seed(tmp_path, body: str, uid: str = "u1") -> OwnerDocWriter:
	root = self_tier_root(tmp_path, uid, "polyrob")
	root.mkdir(parents=True, exist_ok=True)
	(root / "owner.md").write_text(body, encoding="utf-8")
	return OwnerDocWriter(tmp_path)


# --- 1. a patch edits the ACTIVE rules only ---------------------------------

def test_patch_matches_the_active_rules_only(tmp_path):
	w = _seed(tmp_path, _prod_shaped_doc())
	before_sup = superseded_entries(w.read("u1"))
	res = w.patch(user_id="u1", old_string="Supersedes BUYBACKS RESUMED (2026-10-07 05:45).",
	              new_string="Supersedes BUYBACKS RESUMED (2026-10-07 05:45). Cron 45e7a8066a94 cancelled.",
	              created_by="user", pending=False)
	assert res.ok, res.errors
	active, _sup = split_superseded(w.read("u1"))
	assert "45e7a8066a94" in active
	after_sup = superseded_entries(w.read("u1"))
	# history is untouched: every old entry is still there, byte for byte
	for entry in before_sup:
		assert entry in after_sup


def test_replace_all_never_rewrites_superseded_history(tmp_path):
	w = _seed(tmp_path, _prod_shaped_doc())
	before_sup = superseded_entries(w.read("u1"))
	res = w.patch(user_id="u1", old_string="4def3260d811", new_string="45e7a8066a94",
	              replace_all=True, created_by="user", pending=False)
	assert res.ok, res.errors
	active, _sup = split_superseded(w.read("u1"))
	assert "45e7a8066a94" in active and "4def3260d811" not in active
	after_sup = superseded_entries(w.read("u1"))
	for entry in before_sup:
		assert entry in after_sup, "a patch rewrote a Superseded entry"
	# only the ONE replaced active line is new history; no rewritten copies
	assert len(after_sup) == len(before_sup) + 1, after_sup


def test_a_phrase_only_in_superseded_is_not_found_and_says_why(tmp_path):
	w = _seed(tmp_path, _prod_shaped_doc())
	res = w.patch(user_id="u1", old_string="FIXED SIZE 0.05 ETH", new_string="x",
	              created_by="user", pending=False)
	assert not res.ok
	assert "Superseded" in res.errors[0]
	assert "## Superseded" in w.read("u1") and "FIXED SIZE 0.05 ETH" in w.read("u1")


def test_ambiguous_error_counts_the_active_matches(tmp_path):
	doc = "RULE A (2026-10-01): cron abc.\n\nRULE B (2026-10-02): cron abc.\n"
	w = _seed(tmp_path, doc + "\n## Superseded\n\n- old cron abc — superseded 2026-09-01\n")
	res = w.patch(user_id="u1", old_string="cron abc", new_string="cron xyz",
	              created_by="user", pending=False)
	assert not res.ok
	assert "2×" in res.errors[0] and "active" in res.errors[0]


def test_self_doc_patch_is_unchanged(tmp_path):
	"""The base SELF writer has no Superseded section; the whole doc stays patchable."""
	from core.self_context_writer import SelfContextWriter
	w = SelfContextWriter(tmp_path)
	assert w.propose("note one\nnote two\n", user_id="u1", created_by="user", pending=False).ok
	assert w.patch(user_id="u1", old_string="note two", new_string="note 2",
	               created_by="user", pending=False).ok
	assert "note 2" in w.read("u1")


# --- 2. Superseded never holds the same entry twice ------------------------

def test_carry_does_not_write_an_entry_that_already_exists():
	old = "- Post daily at 15:30.\n"
	once = carry_superseded(old, "- Post daily at 16:00.\n", observed_at="2026-10-01")
	# the same retirement again (e.g. a pending draft promoted a day later)
	again = carry_superseded(old + "\n" + SUPERSEDED_HEADING + "\n\n"
	                         + "\n".join(superseded_entries(once)) + "\n",
	                         "- Post daily at 16:00.\n", observed_at="2026-10-02")
	assert len(superseded_entries(again)) == 1


def test_existing_duplicates_collapse_on_the_next_write():
	entry = "- RULE X (2026-10-07) — superseded {d} by: RULE Y"
	doc = ("- RULE Y\n\n" + SUPERSEDED_HEADING + "\n\n"
	       + "\n".join([entry.format(d="2026-10-09"), entry.format(d="2026-10-10"),
	                    entry.format(d="2026-10-09"), "- RULE Z — superseded 2026-10-01"]) + "\n")
	out = carry_superseded(doc, "- RULE Y\n", observed_at="2026-10-11")
	assert superseded_entries(out) == [entry.format(d="2026-10-09"),
	                                   "- RULE Z — superseded 2026-10-01"]


# --- 3. the cap -------------------------------------------------------------

def test_cap_has_headroom_over_the_prod_fill():
	# prod 2026-10-11: active 15,899 chars, 5 refusals at 16000.
	assert OWNER_DOC_MAX_CHARS >= 24000


def test_over_cap_error_names_the_largest_active_rules(tmp_path):
	w = OwnerDocWriter(tmp_path)
	big = "BIG RULE (2026-10-01): " + "x" * (OWNER_DOC_MAX_CHARS + 10) + "\n"
	res = w.propose("SMALL RULE (2026-10-02): ok.\n\n" + big, user_id="u1",
	                created_by="user", pending=False)
	assert not res.ok
	assert "BIG RULE" in res.errors[0]


def test_over_cap_doc_loads_truncated_with_a_warning_not_blocked(tmp_path):
	"""The fail-closed cliff: an over-cap doc used to drop EVERY owner rule."""
	line = "Owner prefers short answers.\n"
	_seed(tmp_path, line * (OWNER_DOC_MAX_CHARS // len(line) + 10))
	loaded = load_owner_doc(tmp_path, "u1", "polyrob")
	assert "[BLOCKED" not in loaded
	assert "TRUNCATED" in loaded and "short answers" in loaded
	assert len(loaded) <= OWNER_DOC_MAX_CHARS + 400


def test_identity_scan_still_blocks_an_over_cap_doc(tmp_path, monkeypatch):
	import modules.memory.task.threat_scan as ts
	monkeypatch.setattr(ts, "is_identity_suspicious", lambda _t: True)
	_seed(tmp_path, "x\n" * (OWNER_DOC_MAX_CHARS))
	assert "[BLOCKED" in load_owner_doc(tmp_path, "u1", "polyrob")


# --- 4. the agent can find its identity docs --------------------------------

@pytest.mark.asyncio
async def test_owner_doc_read_names_the_file_and_its_siblings(monkeypatch, tmp_path):
	import agents.task.agent.service  # noqa: F401 — avoid import cycle
	import logging
	from tools.controller.registry.service import Registry
	from tools.controller.service import Controller
	monkeypatch.setenv("OWNER_DOC_WRITABLE", "true")
	c = object.__new__(Controller)
	c.logger = logging.getLogger("owner-doc-test")
	c.registry = Registry()
	c.user_id = "u1"
	c.session_id = "s1"
	c.container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
	c._register_owner_doc_manage_action()
	action = c.registry.registry.actions["owner_doc_manage"]
	assert "identity" in action.description and "owner.md" in action.description
	ctx = types.SimpleNamespace(user_id="u1", is_sub_agent=False, role="orchestrator")
	rd = await action.function(action.param_model(action="read"), execution_context=ctx)
	text = rd.extracted_content
	assert "owner.md" in text and "self.md" in text
	assert str(self_tier_root(tmp_path, "u1", "polyrob")) in text
