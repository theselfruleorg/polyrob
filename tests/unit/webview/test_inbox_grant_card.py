"""The web Inbox approves a money grant from the FULL grant card (CR-M01).

Before this, the chat push carried ``render_grant_card`` — every money, asset
and target field — and the web Inbox carried a 160-char preview of a 300-char
JSON summary, so a field past that cut was approved blind from the web.
"""
import asyncio

import pytest
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.task.goals.board import GoalBoard
from core.surfaces.inbox import Item
from tools.controller import approval_queue as aq

# A money field placed AFTER a long padding field, so the old 160-char preview
# (and the 300-char params_summary) never reached it.
PARAMS = {
    "chain": "base",
    "description": "x" * 400,
    "max_spend_usd": 4242.5,
    "spend_max_raw": 987654321987,
    "to": "0x" + "ab" * 20,
}


def _ask(board, user_id="u1"):
    return board.create_ask(
        user_id=user_id, what="Approve defi_trade_call? [abc]", why="w",
        extra_payload={
            "ask_kind": aq.TOOL_APPROVAL_ASK_KIND,
            "tool_name": "defi_trade_call",
            "params_summary": aq._params_summary(PARAMS),
            "params": aq._card_params(PARAMS),
            "request_hash": "abc",
        },
        force=True)


def test_pending_row_carries_the_full_card(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    _ask(board)
    rows = aq.list_pending_tool_approvals(board, "u1")
    assert len(rows) == 1
    assert "987654321987" not in rows[0]["preview"]
    card = rows[0]["card"]
    assert "987654321987" in card and "4242.5" in card
    assert "0x" + "ab" * 20 in card
    # The web seat draws its own buttons: no chat reply lines on its card.
    assert "/approve" not in card


def test_a_row_without_stored_params_has_no_card(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    board.create_ask(user_id="u1", what="Approve x?", why="w",
                     extra_payload={"ask_kind": aq.TOOL_APPROVAL_ASK_KIND,
                                    "tool_name": "x", "params_summary": "{}"},
                     force=True)
    assert aq.list_pending_tool_approvals(board, "u1")[0]["card"] == ""


def test_too_big_params_are_not_stored():
    assert aq._card_params({"bytecode": "ab" * aq._CARD_PARAMS_MAX_CHARS}) is None


@pytest.mark.asyncio
async def test_the_queue_writes_the_params_on_its_ask(tmp_path, monkeypatch):
    """The real ``request()`` path stores the params the web card renders."""
    from agents.task.goals.board import ASK_OPEN
    from tools.controller.execution_context import ActionExecutionContext
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    board = GoalBoard(str(tmp_path / "goals.db"))
    provider = aq.OwnerQueueApprover(board=board, poll_interval=0.02,
                                     container=None)
    ctx = ActionExecutionContext(session_id="s1", user_id="u1",
                                 role="orchestrator")
    task = asyncio.create_task(provider.request("defi_trade_call", dict(PARAMS), ctx))
    try:
        for _ in range(50):
            await asyncio.sleep(0.02)
            if board.asks(user_id="u1", status=ASK_OPEN):
                break
        rows = aq.list_pending_tool_approvals(board, "u1")
        assert rows and "987654321987" in rows[0]["card"]
    finally:
        task.cancel()


def test_collector_puts_the_card_on_the_item(tmp_path):
    from surfaces.inbox_sources import collect_tool_approvals
    board = GoalBoard(str(tmp_path / "goals.db"))
    _ask(board)
    items = collect_tool_approvals("u1", data_dir=str(tmp_path))
    card_items = [i for i in items if i.kind == aq.TOOL_APPROVAL_ASK_KIND]
    assert card_items and "987654321987" in card_items[0].extra["card"]


@pytest.fixture()
def page_client(monkeypatch):
    import webview.pages_new as mod
    monkeypatch.setattr(mod, "_pause_headline", lambda: "")
    monkeypatch.setattr(mod, "_read_only", lambda: False)
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app), mod


def test_the_web_card_shows_a_money_field_the_preview_cut(page_client, monkeypatch):
    from core.surfaces.inbox import compose
    client, mod = page_client
    card = aq.pending_grant_card(
        {"tool_name": "defi_trade_call", "params": PARAMS}, "tap-abc")
    body = compose([Item(kind="tool_approval", id="tap-abc",
                         title="defi_trade_call", body="defi_trade_call — …",
                         actions=("approve", "reject"),
                         extra={"card": card})],
                   {"tool_approvals": "ok"})
    monkeypatch.setattr(mod, "_inbox_summary", lambda request: body)
    resp = client.get("/inbox")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    shown = soup.select_one(".entry-card")
    assert shown is not None
    assert "987654321987" in shown.get_text()
    assert "spend_max_raw" in shown.get_text()
