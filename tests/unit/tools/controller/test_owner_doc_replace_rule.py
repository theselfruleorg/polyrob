"""F4 (prod review 2026-10-29): owner.md sat at 97.5 % of its cap, so a new owner
rule could not be added, and the model appended instead of replacing the rule
the directive changed. Nothing from the 10-26 / 10-28 directives reached owner.md.

``owner_doc_manage`` now:
- replaces ONE rule in place, found by its first words (``action='replace_rule'``):
  the old text moves under ``## Superseded`` and stops counting toward the cap;
- reports the cap headroom after every write, and near the cap says to replace
  the superseded rule instead of appending;
- says plainly when NOTHING was written.
"""
import logging
import types

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from core.doc_claims import split_superseded
from core.instance import OWNER_DOC_MAX_CHARS
from tools.controller.registry.service import Registry
from tools.controller.service import Controller

BOUNTY_OLD = ("BOUNTY — JUDGE + PAY ONLY, NO NEW ANNOUNCEMENTS (2026-10-14): the "
              "announce leg is cancelled; judge and pay the open round only.")
LINK = "LINK RULE (2026-09-18): polyrob.example in all comms."
TZ = "## Owner\nTimezone: UTC+2."
BOUNTY_NEW = ("BOUNTY V2 (2026-10-26): $150 per round in PNL, every 3 days, 48 h entry "
              "window; judge right after the window closes.")


def _action(monkeypatch, tmp_path, body=None):
    monkeypatch.setenv("OWNER_DOC_WRITABLE", "true")
    c = object.__new__(Controller)
    c.logger = logging.getLogger("owner-doc-replace")
    c.registry = Registry()
    c.user_id = "u1"
    c.session_id = "s1"
    c.container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    c._register_owner_doc_manage_action()
    if body is not None:
        root = tmp_path / "identity" / "polyrob" / "user_u1"
        root.mkdir(parents=True, exist_ok=True)
        (root / "owner.md").write_text(body, encoding="utf-8")
    return c.registry.registry.actions["owner_doc_manage"]


CTX = types.SimpleNamespace(user_id="u1", is_sub_agent=False, role="orchestrator")


def _draft(tmp_path):
    return (tmp_path / "identity" / "polyrob" / "user_u1" / ".pending" / "owner.md").read_text()


@pytest.mark.asyncio
async def test_replace_rule_supersedes_the_rule_its_first_words_name(monkeypatch, tmp_path):
    action = _action(monkeypatch, tmp_path, f"{TZ}\n\n{BOUNTY_OLD}\n\n{LINK}\n")
    res = await action.function(
        action.param_model(action="replace_rule", rule_start="BOUNTY — JUDGE",
                           new_string=BOUNTY_NEW, source="owner said"),
        execution_context=CTX)
    assert not res.error, res.error
    active, sup = split_superseded(_draft(tmp_path))
    assert "BOUNTY V2" in active and "NO NEW ANNOUNCEMENTS" not in active
    assert "NO NEW ANNOUNCEMENTS" in sup                  # history, not evicted
    assert LINK in active and "Timezone" in active        # nothing else touched
    assert "NOT YET IN EFFECT" in res.extracted_content


@pytest.mark.asyncio
async def test_replace_rule_with_no_text_retires_the_rule(monkeypatch, tmp_path):
    action = _action(monkeypatch, tmp_path, f"{BOUNTY_OLD}\n\n{LINK}\n")
    res = await action.function(
        action.param_model(action="replace_rule", rule_start="bounty — judge + pay"),
        execution_context=CTX)
    assert not res.error, res.error
    active, sup = split_superseded(_draft(tmp_path))
    assert "BOUNTY" not in active and "NO NEW ANNOUNCEMENTS" in sup


@pytest.mark.asyncio
async def test_replace_rule_refuses_an_unknown_or_ambiguous_start(monkeypatch, tmp_path):
    action = _action(monkeypatch, tmp_path, f"{BOUNTY_OLD}\n\nBOUNTY SPLIT: 5 to 7 winners.\n")
    res = await action.function(
        action.param_model(action="replace_rule", rule_start="RELEASES", new_string="x"),
        execution_context=CTX)
    assert res.error and "Nothing was written" in res.error and "no active rule" in res.error
    res = await action.function(
        action.param_model(action="replace_rule", rule_start="BOUNTY", new_string="x"),
        execution_context=CTX)
    assert res.error and "Nothing was written" in res.error and "2 active rules" in res.error


@pytest.mark.asyncio
async def test_a_write_reports_the_headroom(monkeypatch, tmp_path):
    action = _action(monkeypatch, tmp_path)
    res = await action.function(action.param_model(action="update", content=LINK),
                                execution_context=CTX)
    assert f"/{OWNER_DOC_MAX_CHARS:,} chars" in res.extracted_content
    assert "left" in res.extracted_content


@pytest.mark.asyncio
async def test_near_the_cap_the_result_says_replace_not_append(monkeypatch, tmp_path):
    filler = "\n\n".join(f"RULE {i}: " + "w" * 180 for i in range(int(OWNER_DOC_MAX_CHARS * 0.93) // 195))
    action = _action(monkeypatch, tmp_path, filler + "\n")
    res = await action.function(
        action.param_model(action="patch", old_string="RULE 0: ", new_string="RULE 0 (kept): "),
        execution_context=CTX)
    assert not res.error, res.error
    assert "replace_rule" in res.extracted_content
    assert "append" in res.extracted_content.lower()


@pytest.mark.asyncio
async def test_an_over_cap_write_says_nothing_was_written_and_how_to_make_room(monkeypatch, tmp_path):
    action = _action(monkeypatch, tmp_path)
    res = await action.function(
        action.param_model(action="update", content="x " * OWNER_DOC_MAX_CHARS),
        execution_context=CTX)
    assert res.error.startswith("Nothing was written.")
    assert "replace_rule" in res.error


# --- review M3: a list block is several rules; a heading is never a rule ------

PAYMENTS = ("## Payments\n- Pay 5 USDC per bounty winner.\n- Never pay before judging.\n"
            "- Max 7 winners.")


def test_find_rule_in_a_list_names_one_item_and_never_the_heading():
    from tools.controller.doc_authoring import find_rule
    assert find_rule(PAYMENTS, "Pay 5 USDC") == ["- Pay 5 USDC per bounty winner."]
    assert find_rule(PAYMENTS, "Payments") == []          # a heading is not a rule
    assert find_rule("- a rule\n  wrapped on\n- b rule", "a rule") == ["- a rule\n  wrapped on"]


def test_find_rule_keeps_a_blank_line_prose_rule_whole_without_its_heading():
    from tools.controller.doc_authoring import find_rule
    body = f"{TZ}\n\n{BOUNTY_OLD}\nsecond line of the same rule.\n\n{LINK}"
    assert find_rule(body, "BOUNTY — JUDGE") == [BOUNTY_OLD + "\nsecond line of the same rule."]
    assert find_rule(body, "Timezone") == ["Timezone: UTC+2."]


@pytest.mark.asyncio
async def test_replace_rule_in_a_list_replaces_only_that_bullet(monkeypatch, tmp_path):
    action = _action(monkeypatch, tmp_path, f"{PAYMENTS}\n\n{LINK}\n")
    res = await action.function(
        action.param_model(action="replace_rule", rule_start="Pay 5 USDC",
                           new_string="Pay 10 USDC per bounty winner."),
        execution_context=CTX)
    assert not res.error, res.error
    active, sup = split_superseded(_draft(tmp_path))
    assert "## Payments" in active
    assert "- Pay 10 USDC per bounty winner." in active
    assert "- Never pay before judging." in active and "- Max 7 winners." in active
    assert "Pay 5 USDC" in sup
    assert "Never pay before judging" not in sup and "Max 7" not in sup


def test_a_prose_rule_with_no_blank_line_stops_at_the_next_heading():
    from tools.controller.doc_authoring import find_rule
    doc = "## Payments\nPay winners weekly in USDC.\n## Trading\nNever use leverage."
    assert find_rule(doc, "Pay winners") == ["Pay winners weekly in USDC."]
