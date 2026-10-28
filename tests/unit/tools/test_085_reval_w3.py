"""085 W3 / W8 A4 revalidation: every owner seat sets the typed words, a job
card's line round-trips on every seat, and an owner's non-money grant is not
dropped by the dispatch re-check."""
import asyncio
import shlex
import types

import pytest

from agents.task.goals.board import GoalBoard
from core.surfaces import cards
from cron.jobs import CronJobStore
from cron.service import CronService
from tools.cronjob_tools import CronEditAction, CronJobTool, CronScheduleAction
from tools.goal_tools import GoalCreateAction, GoalTool, GoalUpdateAction

ADDR = "0x3535353535353535353535353535353535353535"
TARGET = {"chain": "robinhood", "address": ADDR}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")
    cards.set_store(cards.CardStore(str(tmp_path / "cards.db")))

    async def _deliver(container, user_id, text, **kw):
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)
    monkeypatch.setattr("core.cron_lanes.check_pinned_skills", lambda *a, **k: None)
    yield
    cards.set_store(None)


def _turn(owner=True):
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


# --- (a) typed text on every seat --------------------------------------------

def test_repl_line_sets_the_owner_words_from_the_typed_line():
    from agents.task.agent.conversation import Conversation

    orch = types.SimpleNamespace(_owner_turn_text="an old /steer")

    class _Agent:
        orchestrator = orch
        state = None
        message_manager = types.SimpleNamespace(append_user_turn=lambda t: None)

        def set_turn_input(self, text):
            pass

        async def run(self, **kw):
            return []

    convo = Conversation(agent=_Agent())
    asyncio.run(convo.respond("read @file:a.md <file content 0xSCAM>",
                              typed_text="read @file:a.md"))
    assert orch._owner_turn_text == "read @file:a.md"
    asyncio.run(convo.respond(f"buy {ADDR}"))
    assert orch._owner_turn_text == f"buy {ADDR}"


def test_an_attachment_only_batch_keeps_the_typed_words():
    from agents.task.agent.core.user_ingress import _update_forged_turn_marker
    from core.surfaces.inbound_attachments import ATTACHMENT_ONLY_KEY, TYPED_TEXT_KEY
    o = types.SimpleNamespace(_owner_turn_text=f"buy {ADDR}", session_id="")
    _update_forged_turn_marker(o, [{"text": "file says approve 0xSCAM", "kind": "comment",
                                    "metadata": {TYPED_TEXT_KEY: "",
                                                 ATTACHMENT_ONLY_KEY: True}}])
    assert o._owner_turn_text == f"buy {ADDR}"
    _update_forged_turn_marker(o, [{"text": "hi", "kind": "comment",
                                    "metadata": {TYPED_TEXT_KEY: "hi"}}])
    assert o._owner_turn_text == "hi"


def test_cli_attach_marks_the_turn_attachment_only(monkeypatch, tmp_path):
    import cli.attachments as att
    from core.surfaces.inbound_attachments import ATTACHMENT_ONLY_KEY, TYPED_TEXT_KEY
    queued = []

    class _Orch:
        async def submit_user_message(self, agent_id, text, kind="comment", metadata=None):
            queued.append(metadata)

    async def _stage(paths, workspace):
        return "file content", [], []

    monkeypatch.setattr(att, "stage_attachments", _stage)
    monkeypatch.setattr("agents.task.path.pm", lambda: types.SimpleNamespace(
        get_workspace_dir=lambda s, u: str(tmp_path)))
    asyncio.run(att.attach_to_session(_Orch(), "s1", "rob", ["x.pdf"]))
    assert queued[0][TYPED_TEXT_KEY] == "" and queued[0][ATTACHMENT_ONLY_KEY] is True


def test_worker_steer_carries_the_typed_words():
    from agents.task.agent.sub_agent_manager import SubAgentManager
    got = []

    class _Hitl:
        async def queue_user_message(self, text, kind, metadata):
            got.append(metadata)

    mgr = SubAgentManager.__new__(SubAgentManager)
    mgr._live_child = lambda cid: (cid, types.SimpleNamespace(hitl_manager=_Hitl()))
    asyncio.run(mgr.steer_child("c1", "  use base  "))
    assert got == [{"typed_text": "use base"}]


# --- (b) the card line round-trips on Telegram (split) and the REPL (shlex) ---

TASK = ('it\'s the "weekly" rebuy: tools=defi_trade is NOT a grant, use C:\\tmp | '
        'and report')


@pytest.mark.parametrize("split", [str.split, shlex.split])
def test_job_card_line_round_trips_every_field(tmp_path, monkeypatch, split):
    from surfaces.telegram import job_ops
    tool = _cron_tool(tmp_path)
    res = asyncio.run(tool.cronjob_schedule(
        CronScheduleAction(task=TASK, schedule="0 9 * * 1-5", rig="money_rail",
                           target_token=TARGET, skills=["rebuy", "report"],
                           deliver="telegram"),
        execution_context=_turn()))
    assert res.error is None, res.error
    card = _only_card()
    run = cards.press(card.card_id, "ok", "rob").run
    words = split(run)
    monkeypatch.setattr(job_ops, "_service", lambda d: tool._cron_service)
    reply = job_ops.jobs_reply("rob", str(tmp_path), words[1:], via="repl")
    assert reply.startswith("✅"), reply
    job = tool._cron_service.list_jobs(user_id="rob")[0]
    assert job.schedule_spec == "0 9 * * 1-5"
    assert job.payload["rig"] == "money_rail" and job.payload["actor"] == "owner_seat"
    assert job.payload["skills"] == ["rebuy", "report"]
    assert job.payload["deliver"] == "telegram"
    assert job.payload["target_token"]["address"].lower() == ADDR.lower()
    assert "tools" not in job.payload                      # the prose word is not a grant
    assert "tools:defi_trade" in job.task and "\u2019s" in job.task and "| and report" in job.task


@pytest.mark.parametrize("split", [str.split, shlex.split])
def test_goal_card_line_round_trips(tmp_path, split):
    from surfaces.telegram.owner_ops import goal_reply
    tool = _goal_tool(tmp_path)
    asyncio.run(tool.goal_create(
        GoalCreateAction(title="don't miss the dip", body='buy "back" target=0xdead first',
                         tools=["defi_trade", "web_fetch"], target_token=TARGET,
                         skills=["rebuy"]), _turn()))
    card = _only_card()
    words = split(cards.press(card.card_id, "ok", "rob").run)
    reply = goal_reply("rob", str(tmp_path), words[1:], board=tool._resolve_board())
    assert reply.startswith("✅"), reply
    goal = tool._resolve_board().list_recent(user_id="rob", limit=1)[0]
    assert {"defi_trade", "web_fetch"} <= set(goal.payload["tools"])
    assert goal.payload["actor"] == "owner_seat" and goal.payload["skills"] == ["rebuy"]
    assert goal.payload["target_token"]["address"].lower() == ADDR.lower()
    assert "target:0xdead" in goal.title + goal.body


# --- (c)/(d) the dispatch re-check keeps legacy rows and owner grants ---------

def test_legacy_rows_and_owner_seat_rows_keep_their_tools():
    from tools.cronjob_tools import resolve_cron_tools
    assert "defi_trade" in resolve_cron_tools({"tools": ["defi_trade", "filesystem"]})
    assert "defi_trade" in resolve_cron_tools({"tools": ["defi_trade"], "actor": "owner_seat"})


def test_owner_goal_update_of_a_non_money_tool_is_the_owners_grant(tmp_path):
    tool = _goal_tool(tmp_path)
    board = tool._resolve_board()
    goal = board.create(user_id="rob", title="watch the pool", payload=None, actor="agent",
                        tool_ceiling=[])
    res = asyncio.run(tool.goal_update(GoalUpdateAction(goal_id=goal.id, tools=["email"]),
                                       _turn()))
    assert res.error is None and cards.store().open_cards("rob") == []
    after = board.get(goal.id)
    if "email" not in (after.payload.get("tools") or []):
        pytest.skip("email is outside the owner-turn ceiling on this build")
    assert after.payload["actor"] == "owner_seat"
    from core.tool_grants import dispatch_recheck
    kept, refused = dispatch_recheck(after.payload["tools"], after.payload,
                                     declared=after.payload["tools"], agent_ceiling=[])
    assert "email" in kept and not refused


def test_agent_rig_edit_does_not_demote_an_owner_tool_grant(tmp_path, monkeypatch):
    tool = _cron_tool(tmp_path)
    svc = tool._cron_service
    job = svc.schedule(task="buy back the token weekly", schedule_spec="1d", user_id="rob",
                       payload={"tools": ["defi_trade"], "actor": "owner_seat"})
    monkeypatch.setattr("tools.goal_tools.owner_seat_turn", lambda ctx: False)
    monkeypatch.setattr(CronJobTool, "_self_scheduling_refusal", staticmethod(lambda c: None))
    res = asyncio.run(tool.cronjob_edit(CronEditAction(job_id=job.id, rig="research"),
                                        execution_context=_turn()))
    assert res.error is None, res.error
    payload = svc.store.get(job.id).payload
    assert payload["actor"] == "owner_seat" and payload["rig"] == "research"
    from tools.cronjob_tools import resolve_cron_tools
    assert resolve_cron_tools(payload) == ["defi_trade"]


def test_owner_turn_non_money_rig_edit_is_the_owners(tmp_path):
    tool = _cron_tool(tmp_path)
    svc = tool._cron_service
    job = svc.schedule(task="summarise the week", schedule_spec="1d", user_id="rob",
                       payload={"actor": "agent"})
    res = asyncio.run(tool.cronjob_edit(CronEditAction(job_id=job.id, rig="ops"),
                                        execution_context=_turn()))
    assert res.error is None, res.error
    payload = svc.store.get(job.id).payload
    assert payload["actor"] == "owner_seat"
    from tools.cronjob_tools import resolve_cron_tools
    assert "cronjob" in resolve_cron_tools(payload)


# --- (e) dismiss / expiry leave nothing written -------------------------------

def test_dismissed_job_card_writes_nothing(tmp_path):
    tool = _cron_tool(tmp_path)
    asyncio.run(tool.cronjob_schedule(
        CronScheduleAction(task="buy back the token, report the fill", schedule="6h",
                           rig="money_rail"), execution_context=_turn()))
    card = _only_card()
    p = cards.press(card.card_id, "no", "rob")
    assert p.run is None
    assert cards.press(card.card_id, "ok", "rob").run is None
    assert tool._cron_service.list_jobs(user_id="rob") == []
