"""DEFI-5 / WAL-12: whose target is trust.

A target_token is identity TRUST only when a genuine owner turn's owner message
names the address VERBATIM (or an owner seat wrote it); an address the model
chose is restrict-only.
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


def _turn(owner_text=None):
    meta = {}
    if owner_text is not None:
        meta["owner_text"] = owner_text
    return types.SimpleNamespace(user_id="rob", role="orchestrator", is_sub_agent=False,
                                 metadata=meta, session_id=None, parent_session_id=None)


def _goal(tmp_path, ctx):
    tool = GoalTool.__new__(GoalTool)
    tool._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="buy back the token weekly", target_token=TARGET), ctx))
    assert res.error is None, res.error
    assert "NOT written" not in res.extracted_content, res.extracted_content
    return tool._resolve_board().list_recent(user_id="rob", limit=1)[0].payload


def _job(tmp_path, ctx):
    tool = object.__new__(CronJobTool)
    tool._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    res = asyncio.run(tool.cronjob_schedule(
        CronScheduleAction(task="buy back the token", schedule="0 */6 * * *",
                           target_token=TARGET), execution_context=ctx))
    assert res.error is None, res.error
    assert "NOT written" not in res.extracted_content, res.extracted_content
    return tool._cron_service.list_jobs(user_id="rob")[0].payload


def _trusted(payload) -> bool:
    return with_authorship(payload[PAYLOAD_KEY], payload).get(AUTHOR_KEY) == OWNER


def test_the_owner_typed_the_address_so_it_is_trust():
    from core.wallet.buy_target import target_provenance
    assert target_provenance(_turn(owner_text=f"buy back {ADDR.lower()} every 6h"),
                             TARGET, True) == OWNER


def test_an_address_the_model_chose_is_restrict_only():
    from core.wallet.buy_target import AGENT, target_provenance
    # the owner did not name this contract
    assert target_provenance(_turn(owner_text="buy back PNL every 6h"), TARGET, True) == AGENT


@pytest.mark.parametrize("make", [_goal, _job])
def test_an_owner_turn_target_waits_for_the_owner_card(tmp_path, make, monkeypatch):
    """085 W3 (D3): a target on an owner turn writes no row; the owner's card line
    carries it, and the seat that runs the line stamps owner trust."""
    from core.surfaces import cards
    cards.set_store(cards.CardStore(str(tmp_path / "cards.db")))

    async def _sent(*a, **k):
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _sent)
    try:
        with pytest.raises(AssertionError, match="NOT written"):
            make(tmp_path, _turn(owner_text=f"buy back {ADDR.lower()} every 6h"))
        (card,) = cards.store().open_cards("rob")
        assert f"target={ADDR}" in card.args and "chain=robinhood" in card.args
    finally:
        cards.set_store(None)


def test_the_owner_seat_still_stamps_owner_trust():
    from core.owner_create import cron_options_payload
    _tools, extra = cron_options_payload({"target": ADDR, "chain": "robinhood"})
    assert _trusted(extra)


def test_a_legacy_unstamped_row_is_not_owner_trust():
    # Before the owner stamp existed agent-written rows carried no stamp either,
    # so a target with no stamp can never gain owner trust (fail closed).
    assert not _trusted({PAYLOAD_KEY: dict(TARGET)})


def test_an_agent_stamped_row_is_not_owner_trust():
    assert not _trusted({PAYLOAD_KEY: dict(TARGET), "target_authored_by": "agent"})


def test_the_owner_seat_line_stamps_owner_explicitly():
    # 085 W3: an owner-turn target rides the card's /job new line; its options
    # parser is the seat that stamps the owner.
    from core.owner_create import cron_options_payload
    _tools, extra = cron_options_payload({"target": ADDR, "chain": "robinhood"})
    assert extra["target_authored_by"] == "owner"


@pytest.mark.parametrize("seat", ["goal", "cron"])
def test_owner_create_stamps_owner_and_overrides_a_forged_agent_stamp(tmp_path, seat):
    from core.owner_create import create_cron, create_goal
    extra = {PAYLOAD_KEY: dict(TARGET), "target_authored_by": "agent"}
    if seat == "goal":
        board = GoalBoard(str(tmp_path / "goals.db"))
        payload = create_goal(board, user_id="rob", title="buy back",
                              extra_payload=extra).payload
    else:
        svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
        payload = create_cron(svc, task="buy back", schedule_spec="0 */6 * * *",
                              user_id="rob", extra_payload=extra).payload
    assert payload["target_authored_by"] == "owner"
    assert _trusted(payload)


def test_owner_create_without_a_target_carries_no_stamp(tmp_path):
    from core.owner_create import create_goal
    board = GoalBoard(str(tmp_path / "goals.db"))
    payload = create_goal(board, user_id="rob", title="tidy",
                          extra_payload={"target_authored_by": "owner"}).payload
    assert "target_authored_by" not in (payload or {})
