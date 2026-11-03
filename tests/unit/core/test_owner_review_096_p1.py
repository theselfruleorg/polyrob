"""096 P1 (W1-W3): every owner decision says what changes and why.

Rebuilds the 2026-11-02 notice the owner could not decide from ("+20 / −25
lines — <the new description>") and pins its replacement: Why, the named
changes, and the three taps per item.
"""
import logging
import types

import pytest

from agents.task.agent.skill_manager import SkillManager
from core import pending_reasons, self_evolution
from core.instance import DEFAULT_INSTANCE_ID
from core.owner_doc_writer import OwnerDocWriter
from core.pending_review import (REVIEW_MAX_CHARS, change_summary, review_text,
                                 summary_line, word_edits)

IID = DEFAULT_INSTANCE_ID


# --- W1: the reason store ---------------------------------------------------

def test_reason_round_trip_keeps_newest_last_and_forgets_on_decide(tmp_path):
    for r in ("first", "second", "second", "third", "fourth"):
        pending_reasons.record("u1", "skill", "s", r, by="agent", home_dir=tmp_path,
                               instance_id=IID)
    got = [e["reason"] for e in pending_reasons.reasons("u1", "skill", "s",
                                                        home_dir=tmp_path, instance_id=IID)]
    assert got == ["second", "third", "fourth"]  # no repeat, last three kept
    pending_reasons.forget("u1", "skill", "s", home_dir=tmp_path, instance_id=IID)
    assert pending_reasons.reasons("u1", "skill", "s", home_dir=tmp_path, instance_id=IID) == []


def test_fallback_reason_never_overwrites_the_drafters(tmp_path):
    pending_reasons.record("u1", "skill", "s", "the model's reason", home_dir=tmp_path,
                           instance_id=IID)
    pending_reasons.record("u1", "skill", "s", "run fallback", home_dir=tmp_path,
                           instance_id=IID, only_if_missing=True)
    got = pending_reasons.reasons("u1", "skill", "s", home_dir=tmp_path, instance_id=IID)
    assert [e["reason"] for e in got] == ["the model's reason"]


def test_unreadable_reasons_store_is_said_and_never_overwritten(tmp_path):
    from core.pending_review import why_line
    pending_reasons.record("u1", "skill", "s", "kept", home_dir=tmp_path, instance_id=IID)
    path = pending_reasons._path("u1", tmp_path, IID)
    path.write_text("{not json", encoding="utf-8")
    pending_reasons.record("u1", "skill", "t", "new", home_dir=tmp_path, instance_id=IID)
    pending_reasons.forget("u1", "skill", "s", home_dir=tmp_path, instance_id=IID)
    assert path.read_text(encoding="utf-8") == "{not json"      # nothing wrote over it
    why = pending_reasons.all_reasons("u1", home_dir=tmp_path, instance_id=IID)
    assert pending_reasons.UNREADABLE in why
    assert why_line({"reason": "", "reason_unreadable": True}).startswith("Why: unreadable")


def test_reason_is_one_line_and_capped_with_a_marker():
    out = pending_reasons.clean_reason("a\n b " + "x" * 400)
    assert "\n" not in out and len(out) == pending_reasons.REASON_MAX_CHARS
    assert out.endswith("…")


def test_list_pending_carries_the_reason_and_a_decision_clears_it(tmp_path):
    w = OwnerDocWriter(tmp_path, instance_id=IID)
    assert w.propose("Reply within a day.", user_id="u1", created_by="agent", pending=True).ok
    pending_reasons.record("u1", "owner_doc", "u1", "the owner asked for it today",
                           home_dir=tmp_path, instance_id=IID)
    sm = SkillManager(skills_dir=tmp_path / "skills")
    [item] = self_evolution.list_pending("u1", home_dir=tmp_path, instance_id=IID,
                                         skill_manager=sm)
    assert item["reason"] == "the owner asked for it today" and item["reason_at"] > 0
    ok, _ = self_evolution.reject("owner_doc", "u1", user_id="u1", home_dir=tmp_path,
                                  instance_id=IID, skill_manager=sm, rev=item["rev"])
    assert ok
    assert pending_reasons.reasons("u1", "owner_doc", "u1", home_dir=tmp_path,
                                   instance_id=IID) == []


# --- W2: describe the change -----------------------------------------------

LIVE = ("---\nname: x\ndescription: Before replying on X: read the engagement log\n---\n"
        "# X\n## When to post\nPost at 9.\nNever on Sunday.\n"
        "## Teasers\nOne per day at noon,\nwith a picture.\n## Daily stream\nbye\n")


def test_description_only_edit_names_the_field_and_the_words():
    new = LIVE.replace("engagement log\n", "engagement log first\n")
    c = change_summary(new, LIVE)
    assert c["parts"] == ['description changed: "engagement log" → "engagement log first"']


def test_reflow_reads_as_rewrapped_not_a_line_count():
    new = LIVE.replace("One per day at noon,\nwith a picture.", "One per day at noon, with a picture.")
    c = change_summary(new, LIVE)
    assert c["parts"] == ["§Teasers re-wrapped (same words)"]


def test_sections_added_removed_and_small_edit():
    new = (LIVE.replace("Post at 9.", "Post at 10.")
           .replace("## Daily stream\nbye\n", "## Contests\nOwner approves each one.\n"))
    parts = change_summary(new, LIVE)["parts"]
    assert any(p.startswith("§When to post:") and '"9."' not in p and "10." in p for p in parts)
    assert "added §Contests" in parts and "removed §Daily stream" in parts


def test_a_large_rewrite_is_named_rewrote():
    new = LIVE.replace("Post at 9.\nNever on Sunday.",
                       "Every morning the calendar file decides what goes out, and the owner "
                       "approves each item before it is posted to the account.")
    assert "rewrote §When to post" in change_summary(new, LIVE)["parts"]


def test_word_edits_refuses_a_large_change():
    assert word_edits("a b c", " ".join(["z"] * 30)) is None
    assert word_edits("same words", "same\nwords") == []


def test_new_item_has_no_parts():
    c = change_summary("# New\nbody", None)
    assert c["new"] and c["parts"] == []


def test_owner_doc_change_past_the_review_cap_is_visible():
    live = "# Rules\n" + "\n".join(f"- rule {i} " + "w" * 40 for i in range(400))
    assert len(live) > REVIEW_MAX_CHARS
    new = live + "\n- the new rule at the end"
    out = review_text({"kind": "owner_doc", "id": "u1", "reason": "owner said so"}, new,
                      label="owner-facts note", decide_line="d", is_skill=False,
                      active_body=live)
    assert "+- the new rule at the end" in out
    assert "Why: owner said so" in out
    assert "not shown here" not in out


def test_review_without_a_reason_says_so():
    out = review_text({"kind": "skill", "id": "x"}, "# A\nbody", label="skill 'x'",
                      decide_line="d", is_skill=True)
    assert "Why: not recorded" in out


def test_pref_change_preview_shows_current_and_proposed(tmp_path):
    from core.prefs import list_pending_pref_changes, propose_pref_change
    ok, _ = propose_pref_change("u1", "approvals.require", None, tmp_path, instance_id=IID)
    if not ok:
        pytest.skip("pref not proposable in this build")
    [it] = list_pending_pref_changes("u1", tmp_path, IID)
    assert "→" in it["preview"] and it["preview"].startswith("approvals.require:")


# --- W3: the notice and the review ------------------------------------------

def _item(**kw):
    base = {"kind": "skill", "id": "promo-6551p", "notes": "drafted by a background run",
            "reason": "the run posted a contest thread without your approval",
            "change": change_summary(
                LIVE.replace("Post at 9.", "Post at 10.").replace(
                    "## Daily stream\nbye\n", "## Contests\nOwner approves each.\n"), LIVE)}
    base.update(kw)
    return base


def test_notice_says_what_why_and_changes_with_three_taps():
    text = self_evolution.build_pending_notification([_item()])
    assert text.startswith("🧠 1 proposed change to how I work")
    assert "1. Skill 'promo-6551p' — update (drafted by a background run)" in text
    assert "Why: the run posted a contest thread without your approval" in text
    assert "Changes: §When to post:" in text and "added §Contests" in text
    tap = self_evolution.pending_tap_token
    it = _item()
    assert (f"{tap('approve', it)} · {tap('reject', it)} · read it: {tap('pending', it)}"
            in text)
    assert "+20" not in text and "lines —" not in text
    assert "polyrob " not in text  # no CLI command on a chat seat


def test_notice_caps_changes_by_meaning():
    c = {"new": False, "parts": [f"added §S{i}" for i in range(6)], "added": 6, "removed": 0}
    text = self_evolution.build_pending_notification([_item(change=c)])
    assert "added §S2; +3 more." in text


def test_linked_pair_is_one_block_naming_both_halves():
    skill = _item(linked="owner_doc:u1")
    owner = {"kind": "owner_doc", "id": "u1", "linked": "skill:promo-6551p",
             "retires": ["Teasers go out at noon every day"],
             "change": {"new": False, "parts": ["removed one rule"], "added": 0, "removed": 1}}
    text = self_evolution.build_pending_notification([skill, owner])
    assert text.startswith("🧠 1 proposed change")
    assert "Skill 'promo-6551p' + the owner-facts note — update" in text
    assert 'Also retires: "Teasers go out at noon every day"' in text


def test_summary_line_for_new_skill_uses_description():
    it = {"kind": "skill", "change": {"new": True, "description": "Do X well", "parts": []}}
    assert summary_line(it) == "new skill — Do X well"


# --- W1/D3: an owner-turn write needs a reason --------------------------------

@pytest.fixture
def skill_call(monkeypatch, tmp_path):
    from tools.controller.registry.service import Registry
    from tools.controller.service import Controller
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "true")
    sm = SkillManager(skills_dir=tmp_path)
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)
    monkeypatch.setattr("core.runtime_paths.data_dir_or_home", lambda *_a, **_k: str(tmp_path))
    c = object.__new__(Controller)
    c.logger = logging.getLogger("t096")
    c.registry = Registry()
    c.user_id = "u1"
    c.session_id = "s1"
    c._register_skill_manage_action()
    action = c.registry.registry.actions["skill_manage"]

    async def call(ctx, **kw):
        return await action.function(action.param_model(**kw), execution_context=ctx)
    return call, tmp_path


BODY = "# Deploy\n\nRun the script, then check the health page twice.\n"
OWNER = types.SimpleNamespace(user_id="u1", is_sub_agent=False, role="orchestrator")
FORGED = types.SimpleNamespace(user_id="u1", is_sub_agent=True, role="leaf")


@pytest.mark.asyncio
async def test_owner_turn_create_without_reason_writes_nothing(skill_call):
    call, home = skill_call
    res = await call(OWNER, action="create", skill_id="deploy", content=BODY, when="deploys")
    assert res.error and "needs `reason`" in res.error
    assert not list(home.rglob("SKILL.md"))


@pytest.mark.asyncio
async def test_owner_turn_reason_is_recorded_for_the_notice(skill_call):
    call, home = skill_call
    res = await call(OWNER, action="create", skill_id="deploy", content=BODY, when="deploys",
                     reason="the last deploy skipped the health check")
    assert res.error is None, res.error
    got = pending_reasons.reasons("u1", "skill", "deploy", home_dir=str(home), instance_id=IID)
    assert got and got[-1]["reason"] == "the last deploy skipped the health check"


@pytest.mark.asyncio
async def test_background_write_without_reason_is_allowed(skill_call):
    call, _home = skill_call
    res = await call(FORGED, action="create", skill_id="deploy", content=BODY, when="deploys")
    assert res.error is None, res.error


def test_doc_queued_reply_has_reject_and_read_taps():
    from tools.controller.doc_authoring import _queued_text
    out = _queued_text("Owner-facts", "owner_doc", "u1", changes="added §Rules")
    it = {"kind": "owner_doc", "id": "u1"}
    tap = self_evolution.pending_tap_token
    assert tap("approve", it) in out and tap("reject", it) in out
    assert f"read it: {tap('pending', it)}" in out
    assert "Changes: added §Rules" in out
