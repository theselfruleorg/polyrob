"""The fallback grant card (render_grant_card raised) still carries ONE tappable
token per verb — ``/approve_tap_<id>`` — never the spaced ``/approve tap-…``
form, whose argument a chat client does not link."""
import asyncio

import pytest

from agents.task.goals.board import ASK_OPEN, GoalBoard
from core.surfaces.tappable import parse_tappable
from tools.controller import approval_queue as aq
from tools.controller import grant_card
from tools.controller.execution_context import ActionExecutionContext


@pytest.mark.asyncio
async def test_fallback_card_uses_the_folded_token(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")

    def _boom(*a, **k):
        raise RuntimeError("render failed")
    monkeypatch.setattr(grant_card, "render_grant_card", _boom)
    pushed = []

    async def _push(container, user_id, text):
        pushed.append(text)
    monkeypatch.setattr(aq, "_push_owner_notification", _push)

    board = GoalBoard(str(tmp_path / "goals.db"))
    provider = aq.OwnerQueueApprover(board=board, poll_interval=0.02, container=None)
    ctx = ActionExecutionContext(session_id="s1", user_id="u1", role="orchestrator")
    task = asyncio.create_task(provider.request("x402_request", {"amount_usd": 5}, ctx))
    try:
        for _ in range(50):
            await asyncio.sleep(0.02)
            if pushed:
                break
    finally:
        task.cancel()
    assert pushed, "no card was pushed"
    card = pushed[0]
    ask = board.asks(user_id="u1", status=ASK_OPEN)[0]
    tokens = [w for w in card.split() if w.startswith(("/approve", "/reject"))]
    assert tokens
    for tok in tokens:
        verb, arg = parse_tappable(tok)
        assert verb in ("/approve", "/reject"), tok
        assert arg == aq.tap_display_id(ask.id)
    assert "/approve tap-" not in card and "/reject tap-" not in card
