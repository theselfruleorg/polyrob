"""096 W7 (R4): one pending renderer on every seat — CLI, REPL, console and
chat show the SAME what · Why · Changes for the same item; only the decide
verbs are the seat's own."""
import pytest

from core import pending_seats, self_evolution
from core.instance import DEFAULT_INSTANCE_ID
from core.owner_doc_writer import OwnerDocWriter

IID = DEFAULT_INSTANCE_ID


def _skill(**kw):
    base = {"kind": "skill", "id": "promo", "notes": "drafted by a background run",
            "reason": "the run posted without approval", "preview": "---\nname: promo\n---",
            "change": {"new": False, "parts": ["§Approval: now asks first"],
                       "added": 1, "removed": 1}}
    base.update(kw)
    return base


def _owner_half():
    return {"kind": "owner_doc", "id": "u1", "linked": "skill:promo",
            "retires": ["Teasers at noon"], "preview": "x",
            "change": {"new": False, "parts": ["removed one rule"], "added": 0, "removed": 1}}


@pytest.mark.parametrize("seat", [pending_seats.SEAT_CHAT, pending_seats.SEAT_CLI,
                                  pending_seats.SEAT_REPL, pending_seats.SEAT_CONSOLE])
def test_every_seat_shows_the_same_what_why_changes(seat):
    lines = pending_seats.item_lines(_skill(), seat=seat)
    text = "\n".join(lines)
    assert "Skill 'promo' — update (drafted by a background run)" in text
    assert "Why: the run posted without approval" in text
    assert "Changes: §Approval: now asks first." in text
    assert "name: promo" not in text  # never the raw frontmatter preview
    chat = self_evolution.notice_item_lines(_skill())
    assert lines[:3] == chat[:3]


def test_decide_verbs_are_the_seats_own():
    it = _skill()
    cli = "\n".join(pending_seats.item_lines(it, seat=pending_seats.SEAT_CLI))
    repl = "\n".join(pending_seats.item_lines(it, seat=pending_seats.SEAT_REPL))
    console = "\n".join(pending_seats.item_lines(it, seat=pending_seats.SEAT_CONSOLE))
    assert "polyrob owner promote skill promo" in cli
    assert "polyrob owner show-pending skill promo" in cli
    assert "/pending approve skill promo" in repl and "/pending show skill promo" in repl
    for text in (cli, repl, console):
        assert "/approve_p_" not in text  # no chat tap token off the chat seat
    assert "polyrob" not in console and "/pending" not in console


def test_a_linked_pair_is_one_row_on_every_list():
    items = [_skill(linked="owner_doc:u1"), _owner_half()]
    assert len(pending_seats.fold_linked(items)) == 1
    text = "\n".join(pending_seats.list_lines(items, seat=pending_seats.SEAT_CLI))
    assert "Skill 'promo' + the owner-facts note" in text
    assert 'Also retires: "Teasers at noon"' in text
    assert "Decide both: polyrob owner promote skill promo" in text


def test_other_kinds_use_the_shared_summary():
    it = {"kind": "tool_approval", "id": "tap-1", "preview": "Post a tweet: hi",
          "card": "Approve: post a tweet\n```\nhi there\n```"}
    text = "\n".join(pending_seats.item_lines(it, seat=pending_seats.SEAT_CLI))
    assert "approval tool_approval:tap-1 — Post a tweet: hi" in text
    ok, review = pending_seats.review_one(it, user_id="u", home_dir="/nope",
                                          instance_id=IID, seat=pending_seats.SEAT_CLI)
    assert ok and review.startswith("Approve: post a tweet") and "hi there" in review
    assert review.endswith("polyrob owner reject tool_approval tap-1")


def test_review_is_the_shared_review_with_the_seats_decide_line(tmp_path):
    from agents.task.agent.skill_manager import SkillManager
    w = OwnerDocWriter(tmp_path, instance_id=IID)
    assert w.propose("Reply within a day.", user_id="u1", created_by="user").ok
    assert w.propose("Reply within an hour.", user_id="u1", created_by="agent",
                     pending=True).ok
    sm = SkillManager(skills_dir=tmp_path / "skills")
    [item] = self_evolution.list_pending("u1", home_dir=tmp_path, instance_id=IID,
                                         skill_manager=sm)
    ok, text = pending_seats.review_one(item, user_id="u1", home_dir=tmp_path,
                                        instance_id=IID, seat=pending_seats.SEAT_REPL,
                                        skill_manager=sm)
    assert ok
    assert text.startswith("Pending owner-facts note") and "Why:" in text
    # 098 CO-19: the typed seat decides the revision it shows.
    assert text.rstrip().endswith(f"/pending reject owner_doc u1@{item['rev']}")
    assert "/approve_p_" not in text


def test_console_inbox_card_uses_the_shared_lines(tmp_path, monkeypatch):
    from surfaces import inbox_sources
    monkeypatch.setattr(self_evolution, "list_pending",
                        lambda *a, **k: [_skill(linked="owner_doc:u1"), _owner_half()])
    [card] = inbox_sources.collect_self_evolution("u1", data_dir=str(tmp_path),
                                                  instance_id=IID)
    assert card.title.startswith("Skill 'promo' + the owner-facts note — update")
    assert "Why: the run posted without approval" in card.body
    assert "name: promo" not in card.title + card.body
    assert "show" in card.actions


def test_console_show_action_becomes_a_read_button():
    from webview.pages_new import _card
    row = _card({"kind": "skill", "id": "promo", "source": "self_evolution",
                 "actions": ("approve", "reject", "show")})
    read = [a for a in row["rendered_actions"] if a.get("read")]
    assert len(read) == 1 and read[0]["verb"] is None
