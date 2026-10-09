"""DEFI-5 / WAL-12 / AGT-1: who authors standing work, and whose target is trust.

ONE rule (``tools.goal_tools.owner_authored_turn``): a genuine owner turn that
has read no third-party content writes an OWNER-authored goal or cron job; any
other turn (a forged/autonomous turn, or an owner turn after a web page, a mail
or a tool result entered it) writes an AGENT-authored one. A target_token is
identity TRUST only on an owner-authored row whose owner message names the
address VERBATIM; an address the model chose is restrict-only.
"""
import asyncio
import types

import pytest

from agents.task.goals.board import GoalBoard
from core.wallet.buy_target import AUTHOR_KEY, OWNER, PAYLOAD_KEY, with_authorship
from cron.jobs import CronJobStore
from cron.service import CronService
from tools.cronjob_tools import CronJobTool, CronScheduleAction
from tools.goal_tools import GoalCreateAction, GoalTool

ADDR = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"
TARGET = {"chain": "robinhood", "address": ADDR}


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")


def _turn(owner_text=None, tainted=False):
    meta = {}
    if owner_text is not None:
        meta["owner_text"] = owner_text
    if tainted:
        meta["untrusted_read"] = True
    return types.SimpleNamespace(user_id="rob", role="orchestrator", is_sub_agent=False,
                                 metadata=meta, session_id=None, parent_session_id=None)


def _goal(tmp_path, ctx):
    tool = GoalTool.__new__(GoalTool)
    tool._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="buy back the token weekly", target_token=TARGET), ctx))
    assert res.error is None, res.error
    return tool._resolve_board().list_recent(user_id="rob", limit=1)[0].payload


def _job(tmp_path, ctx):
    tool = object.__new__(CronJobTool)
    tool._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    res = asyncio.run(tool.cronjob_schedule(
        CronScheduleAction(task="buy back the token", schedule="0 */6 * * *",
                           target_token=TARGET), execution_context=ctx))
    assert res.error is None, res.error
    return tool._cron_service.list_jobs(user_id="rob")[0].payload


def _trusted(payload) -> bool:
    return with_authorship(payload[PAYLOAD_KEY], payload).get(AUTHOR_KEY) == OWNER


@pytest.mark.parametrize("make", [_goal, _job])
def test_the_owner_typed_the_address_so_it_is_trust(tmp_path, make):
    p = make(tmp_path, _turn(owner_text=f"buy back {ADDR.lower()} every 6h"))
    assert p["authored_by"] == "owner" and _trusted(p)


@pytest.mark.parametrize("make", [_goal, _job])
def test_an_address_the_model_chose_is_restrict_only(tmp_path, make):
    p = make(tmp_path, _turn(owner_text="buy back PNL every 6h"))
    assert p["authored_by"] == "owner"       # the owner asked for the job
    assert not _trusted(p)                   # but did not name this contract


@pytest.mark.parametrize("make", [_goal, _job])
def test_a_turn_that_read_third_party_content_writes_agent_work(tmp_path, make):
    p = make(tmp_path, _turn(owner_text=f"buy {ADDR}", tainted=True))
    assert p["authored_by"] == "agent" and not _trusted(p)


def test_the_owner_seat_still_stamps_owner_trust():
    from core.owner_create import cron_options_payload
    _tools, extra = cron_options_payload({"target": ADDR, "chain": "robinhood"})
    assert _trusted(extra)
