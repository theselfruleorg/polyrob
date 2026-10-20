"""080 WS-3 — one decision list.

D T8 (2026-10-13): 11 waiting items sat over 7 lists; ``/inbox`` showed 4 and
said "All of them answered". The Inbox now reads cards and signer approvals
too; ``/pending`` is a VIEW of it; ``/missed`` offers
"raise the cap" only for a capped notice; a title never breaks mid-number.
"""
import os
import time
from types import SimpleNamespace

import pytest

import surfaces.inbox_sources as src
from core.surfaces import cards
from core.surfaces.inbox import Item, build_inbox, view
from core.surfaces.inbox_render import CHAT_REMEDIES, render_inbox


# --- the collectors -------------------------------------------------------- #

def test_default_collectors_read_every_list(tmp_path):
    names = set(src.default_collectors(data_dir=str(tmp_path), instance_id="i"))
    assert {"self_evolution", "tool_approvals", "correspondents", "asks", "apps",
            "signer_approvals", "cards"} <= names


def test_cards_collector_lists_an_open_card_with_its_taps(tmp_path):
    st = cards.CardStore(str(tmp_path / "cards.db"))
    card = cards.choice_card("u1", "Which pool?", ["A", "B"], st=st)
    items = src.collect_cards("u1", data_dir=str(tmp_path))
    assert [i.id for i in items] == [card.card_id]
    assert items[0].source == "cards" and items[0].blocking
    assert f"/card_{card.card_id}_1" in items[0].meta
    assert src.collect_cards("someone-else", data_dir=str(tmp_path)) == []


def test_cards_collector_never_creates_the_store(tmp_path):
    assert src.collect_cards("u1", data_dir=str(tmp_path)) == []
    assert not os.path.exists(tmp_path / "cards.db")


def test_signer_collector_is_empty_with_a_local_signer(monkeypatch, tmp_path):
    import core.signer as signer
    monkeypatch.setattr(signer, "signer_mode", lambda: signer.MODE_LOCAL)
    assert src.collect_signer_approvals("u1", data_dir=str(tmp_path)) == []


def test_signer_collector_lists_held_spends(monkeypatch, tmp_path):
    import core.signer as signer
    import core.signer.approvals as approvals
    monkeypatch.setattr(signer, "signer_mode", lambda: "remote")
    monkeypatch.setattr(approvals, "pending_items", lambda: [
        {"kind": "signer_approval", "id": "s1", "preview": "signer review: $900.00"}])
    items = src.collect_signer_approvals("u1", data_dir=str(tmp_path))
    assert [(i.kind, i.id, i.source) for i in items] == [
        ("signer_approval", "s1", "signer_approvals")]
    assert items[0].actions == ("approve", "reject")


# --- one count, no false "answered" ----------------------------------------- #

def _body(**extra):
    collectors = {"asks": lambda uid: [Item(kind="ask", id="a1", title="an ask",
                                            source="asks")],
                  "cards": lambda uid: [Item(kind="card", id="c1", title="a card",
                                             source="cards")],
                  "tool_approvals": lambda uid: [Item(
                      kind="tool_approval", id="tap-1", title="spend",
                      actions=("approve", "reject"), source="tool_approvals")]}
    collectors.update(extra)
    return build_inbox("u1", collectors)


def test_inbox_counts_every_list_and_does_not_say_all_answered():
    body = _body()
    assert body["count"] == 3
    text = render_inbox(body, remedies=CHAT_REMEDIES, width=60)
    assert "All of them answered" not in text


def test_all_answered_only_over_an_empty_list():
    text = render_inbox(build_inbox("u1", {"asks": lambda uid: []}))
    assert "All of them answered." in text


def test_pending_is_a_view_of_the_inbox():
    body = _body()
    pend = view(body, "pending")
    assert [i["id"] for i in pend["items"]] == ["tap-1"]
    assert pend["count"] == 1 and pend["total"] == 3
    assert [i["id"] for i in view(body, "cards")["items"]] == ["c1"]
    assert [i["id"] for i in view(body, "asks")["items"]] == ["a1"]


def test_a_refused_source_outside_the_view_does_not_taint_it():
    body = _body(cards=lambda uid: (_ for _ in ()).throw(OSError("locked")))
    assert view(body, "pending")["uncertain"] is False
    assert view(body, "cards")["uncertain"] is True


def test_console_pending_endpoint_agrees_with_chat_pending(monkeypatch, tmp_path):
    """D T8: chat /pending said 2 and GET /api/webgate/pending said 4."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import webview.pages as pages
    from agents.task.goals.board import GoalBoard
    from core.surfaces.correspondents import CorrespondentRegistry
    from tools.controller.approval_queue import all_pending

    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "alice")
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pages.webgate, "data_dir", lambda: str(tmp_path))
    board = GoalBoard(str(tmp_path / "goals.db"))
    board.create_ask(user_id="alice", what="Approve x402_request? [abc]",
                     why="tool=x402_request", force=True, extra_payload={
                         "ask_kind": "tool_approval", "tool_name": "x402_request",
                         "request_hash": "abc", "grant_consumed": False})
    board.create_ask(user_id="alice", what="Which key?", why="the run needs one")
    CorrespondentRegistry(str(tmp_path / "correspondents.db")).seed(
        surface="email", address="bob@example.com", session_id="s",
        user_id="alice", require_approval=True)
    app = FastAPI()
    app.include_router(pages.router)
    api = TestClient(app).get("/api/webgate/pending").json()
    chat = all_pending(user_id="alice", home_dir=str(tmp_path), instance_id="i",
                       board=board)
    assert api["count"] == len(chat.items) == 2
    assert sorted(i["kind"] for i in api["items"]) == ["correspondent", "tool_approval"]


# --- a title never breaks mid-number ---------------------------------------- #

def test_a_long_title_wraps_and_keeps_the_whole_amount():
    title = ("Approve x402_pay? [abc123] url=https://api.example.com/v1/quote "
             "max_usd=2.50 reason=price feed")
    body = build_inbox("u1", {"tool_approvals": lambda uid: [Item(
        kind="tool_approval", id="tap-1", title=src._first_line(title),
        created_at=time.time() - 7200, source="tool_approvals",
        actions=("approve", "reject"))]})
    text = render_inbox(body, remedies=CHAT_REMEDIES, width=60)
    assert "max_usd=2.50" in text
    assert "waiting 2h" in text
    assert all(len(line) <= 60 for line in text.splitlines())


def test_first_line_cuts_at_a_word_boundary():
    title = src._first_line("word " * 30 + "max_usd=2.50")
    assert title.endswith("…") and "max_usd=2." not in title


# --- the auto "Unblock goal" ask ------------------------------------------- #

@pytest.fixture()
def card_store(tmp_path):
    st = cards.CardStore(str(tmp_path / "cards.db"))
    cards.set_store(st)
    yield st
    cards.set_store(None)


def _blocked_goal(board, payload, error):
    from agents.task.goals.board import STATUS_BLOCKED
    g = board.create(user_id="u1", title="Trading Goal 2", payload=payload)
    return SimpleNamespace(id=g.id, user_id="u1", title=g.title, payload=payload,
                           last_failure_error=error, status=STATUS_BLOCKED)


def test_auto_unblock_ask_carries_no_card(tmp_path, card_store):
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.board_asks import create_auto_unblock_ask
    board = GoalBoard(str(tmp_path / "goals.db"))
    goal = _blocked_goal(board, {}, "provider 401")
    ask = create_auto_unblock_ask(board, goal)
    assert "fix_verb" not in ask.payload and ask.payload["auto_unblock"] is True
    assert card_store.open_cards("u1") == []


# --- /missed names the cap only when the cap was the cause ------------------ #

def test_missed_offers_the_cap_only_for_a_capped_notice(monkeypatch):
    import core.surfaces.missed as missed
    from surfaces.telegram.status_ops import _missed_reply
    monkeypatch.setattr(missed, "missed_notices", lambda u, d, n: [
        {"ts": 1.0, "kind": "undelivered", "text": "the report"}])
    assert "daily_cap" not in _missed_reply("u1", "/x", [])
    monkeypatch.setattr(missed, "missed_notices", lambda u, d, n: [
        {"ts": 1.0, "kind": "capped", "text": "the report"}])
    assert "daily_cap" in _missed_reply("u1", "/x", [])
