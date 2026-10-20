"""085 W3 (A2 / AGT-1, D3): an owner turn's standing MONEY work needs the owner's tap.

An owner-turn goal_create / goal_update / cronjob_schedule / cronjob_edit that
carries a money tool, a money rig or a buy target writes NO row. It raises a job
card holding the owner seat's own line; Confirm runs that line on the seat,
which writes the row with actor=owner_seat. Work that is not money writes at
once, as before.
"""
import asyncio
import types

import pytest

from agents.task.goals.board import GoalBoard
from core.surfaces import cards
from cron.jobs import CronJobStore
from cron.service import CronService
from tools.cronjob_tools import CronEditAction, CronJobTool, CronScheduleAction
from tools.goal_tools import GoalCreateAction, GoalTool, GoalUpdateAction

ADDR = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"
TARGET = {"chain": "robinhood", "address": ADDR}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")
    cards.set_store(cards.CardStore(str(tmp_path / "cards.db")))
    sent = []

    async def _deliver(container, user_id, text, **kw):
        sent.append((user_id, text, kw))
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)
    yield sent
    cards.set_store(None)


def _owner_turn():
    return types.SimpleNamespace(user_id="rob", role="orchestrator", is_sub_agent=False,
                                 metadata={"owner_text": "set it up"}, session_id=None,
                                 parent_session_id=None)


def _goal_tool(tmp_path):
    tool = GoalTool.__new__(GoalTool)
    tool._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return tool


def _cron_tool(tmp_path):
    tool = object.__new__(CronJobTool)
    tool._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return tool


def _only_card():
    open_ = cards.store().open_cards("rob")
    assert len(open_) == 1
    return open_[0]


def _confirm(card):
    p = cards.press(card.card_id, "ok", "rob")
    assert p.run == card.line and not p.run.endswith(" go")
    return p.run.split()


# --- goals ------------------------------------------------------------------

def test_owner_turn_money_rig_goal_is_a_card_and_no_row(tmp_path, _env):
    tool = _goal_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="buy back the token hourly", rig="money_rail"), _owner_turn()))
    assert res.error is None and "NOT written" in res.extracted_content
    card = _only_card()
    assert card.kind == cards.KIND_JOB and card.card_id in res.extracted_content
    assert card.line.startswith("/goal new buy back the token hourly")
    assert "rig=money_rail" in card.args
    assert tool._resolve_board().list_recent(user_id="rob", limit=5) == []
    assert _env and _env[0][2]["card_id"] == card.card_id      # the owner saw it


def test_confirm_writes_the_goal_with_the_owner_seat(tmp_path):
    from surfaces.telegram.owner_ops import goal_reply
    tool = _goal_tool(tmp_path)
    asyncio.run(tool.goal_create(
        GoalCreateAction(title="buy back the token hourly", tools=["defi_trade"],
                         target_token=TARGET), _owner_turn()))
    card = _only_card()
    words = _confirm(card)
    reply = goal_reply("rob", str(tmp_path), words[1:], board=tool._resolve_board())
    assert reply.startswith("✅"), reply
    goal = tool._resolve_board().list_recent(user_id="rob", limit=1)[0]
    assert goal.payload["actor"] == "owner_seat"
    assert "defi_trade" in goal.payload["tools"]
    assert goal.payload["target_token"]["address"].lower() == ADDR.lower()
    assert goal.payload["target_authored_by"] == "owner"
    assert cards.store().get(card.card_id).state == cards.S_CONFIRMED


def test_non_money_owner_goal_writes_at_once(tmp_path):
    tool = _goal_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="draft the weekly research note", tools=["web_fetch"]),
        _owner_turn()))
    assert res.error is None and "Created goal" in res.extracted_content
    goal = tool._resolve_board().list_recent(user_id="rob", limit=1)[0]
    assert goal.payload["actor"] == "owner_seat"
    assert cards.store().open_cards("rob") == []


def test_owner_goal_update_with_a_money_tool_is_a_card(tmp_path):
    tool = _goal_tool(tmp_path)
    asyncio.run(tool.goal_create(GoalCreateAction(title="watch the pool"), _owner_turn()))
    goal = tool._resolve_board().list_recent(user_id="rob", limit=1)[0]
    res = asyncio.run(tool.goal_update(
        GoalUpdateAction(goal_id=goal.id, tools=["defi_trade"], acceptance="a report"),
        _owner_turn()))
    assert res.error is None and "NOT written" in res.extracted_content
    after = tool._resolve_board().get(goal.id)
    assert "defi_trade" not in (after.payload.get("tools") or [])
    assert after.payload["acceptance"] == "a report"          # the other field landed
    card = _only_card()
    assert card.args[:2] == ["grant", goal.id]
    from surfaces.telegram.owner_ops import goal_reply
    reply = goal_reply("rob", str(tmp_path), _confirm(card)[1:], board=tool._resolve_board())
    assert reply.startswith("✅"), reply
    granted = tool._resolve_board().get(goal.id).payload
    assert granted["tools"] == ["defi_trade"] and granted["actor"] == "owner_seat"


# --- scheduled jobs -----------------------------------------------------------

def test_owner_turn_money_job_is_a_card_and_confirm_schedules_it(tmp_path, monkeypatch):
    from surfaces.telegram.job_ops import jobs_reply
    monkeypatch.setattr("core.cron_lanes.check_pinned_skills", lambda *a, **k: None)
    tool = _cron_tool(tmp_path)
    res = asyncio.run(tool.cronjob_schedule(
        CronScheduleAction(task="buy back the token, report the fill", schedule="0 */6 * * *",
                           rig="money_rail", target_token=TARGET, skills=["buyback"]),
        execution_context=_owner_turn()))
    assert res.error is None and "NOT written" in res.extracted_content
    assert tool._cron_service.list_jobs(user_id="rob") == []
    card = _only_card()
    assert card.line.startswith("/job new 0 */6 * * * | buy back the token")
    words = _confirm(card)
    monkeypatch.setattr("surfaces.telegram.job_ops._service", lambda d: tool._cron_service)
    reply = jobs_reply("rob", str(tmp_path), words[1:], via="telegram")
    assert reply.startswith("✅"), reply
    job = tool._cron_service.list_jobs(user_id="rob")[0]
    assert job.payload["rig"] == "money_rail" and job.payload["actor"] == "owner_seat"
    assert job.payload["target_authored_by"] == "owner"
    assert job.payload["skills"] == ["buyback"]


def test_non_money_owner_job_writes_at_once(tmp_path):
    tool = _cron_tool(tmp_path)
    res = asyncio.run(tool.cronjob_schedule(
        CronScheduleAction(task="summarise the week for the owner", schedule="1d",
                           rig="research"),
        execution_context=_owner_turn()))
    assert res.error is None and "Scheduled" in res.extracted_content
    job = tool._cron_service.list_jobs(user_id="rob")[0]
    assert job.payload["actor"] == "owner_seat"
    assert cards.store().open_cards("rob") == []


def test_owner_job_edit_to_a_money_rig_is_a_card(tmp_path, monkeypatch):
    from surfaces.telegram import job_ops
    tool = _cron_tool(tmp_path)
    asyncio.run(tool.cronjob_schedule(
        CronScheduleAction(task="summarise the week for the owner", schedule="1d"),
        execution_context=_owner_turn()))
    job = tool._cron_service.list_jobs(user_id="rob")[0]
    res = asyncio.run(tool.cronjob_edit(CronEditAction(job_id=job.id, rig="money_rail"),
                                        execution_context=_owner_turn()))
    assert res.error is None and "NOT written" in res.extracted_content
    assert "rig" not in (tool._cron_service.store.get(job.id).payload or {})
    card = _only_card()
    assert card.args[:3] == ["edit", job.id, "grant"]
    monkeypatch.setattr(job_ops, "_service", lambda data_dir: tool._cron_service)
    reply = job_ops.jobs_reply("rob", str(tmp_path), _confirm(card)[1:], via="telegram")
    assert reply.startswith("✅"), reply
    assert tool._cron_service.store.get(job.id).payload["rig"] == "money_rail"


def test_a_plain_owner_job_edit_writes_at_once(tmp_path):
    tool = _cron_tool(tmp_path)
    asyncio.run(tool.cronjob_schedule(
        CronScheduleAction(task="summarise the week for the owner", schedule="1d"),
        execution_context=_owner_turn()))
    job = tool._cron_service.list_jobs(user_id="rob")[0]
    res = asyncio.run(tool.cronjob_edit(CronEditAction(job_id=job.id, schedule="2d"),
                                        execution_context=_owner_turn()))
    assert res.error is None and "Edited" in res.extracted_content
    assert cards.store().open_cards("rob") == []


# --- the card itself ----------------------------------------------------------

def test_a_job_card_carries_only_the_seat_write_verbs(tmp_path):
    st = cards.CardStore(str(tmp_path / "c2.db"))
    for bad in (["/send", "1", "native"], ["/goal", "stop", "abc"], ["/job", "run", "abc"],
                ["/job", "edit", "abc", "schedule", "1d"], ["/wallet", "autonomous", "500"]):
        with pytest.raises(ValueError):
            cards.job_card("rob", bad, title="t", st=st)
    card = cards.job_card("rob", ["/job", "new", "1d", "|", "go", "execute", "it"],
                          title="t", st=st)
    assert [a.label for a in cards.card_actions(card)] == ["Confirm", "Dismiss"]
    assert cards.press(card.card_id, "re", "rob", st=st).run is None
    assert cards.press(card.card_id, "ok", "other", st=st).run is None   # one owner
    first = cards.press(card.card_id, "ok", "rob", st=st)
    assert first.run == "/job new 1d | go execute it"
    assert cards.press(card.card_id, "ok", "rob", st=st).run is None     # once


def test_card_words_strip_what_a_command_cannot_carry():
    assert cards.card_words("buy `X` <now>\x07ok") == ["buy", "\u2019X\u2019", "(now)", "ok"]
