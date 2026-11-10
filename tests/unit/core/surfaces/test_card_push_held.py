"""A card push the hourly message limit held collapses into ONE line (prod 10-19/10-28).

14 card pushes came back ``rate_limited``: the cards sat in ``/cards`` and the
owner got nothing, so he had to think of opening ``/cards`` himself. Now the
held pushes of a burst become one "N more cards wait in /cards" line, sent on the
exempt lane (it neither spends nor is denied by the budget), once per window.
"""
import asyncio

import pytest

from core.surfaces import cards


@pytest.fixture
def rail(monkeypatch):
    sent = []

    async def _deliver(container, user_id, text, **kw):
        sent.append((text, kw))
        return "rate_limited" if kw.get("card_id") else "sent"
    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)
    monkeypatch.setattr(cards, "HELD_PUSH_DELAY_S", 0.0)
    cards._HELD_PUSHES.clear()
    yield sent
    cards._HELD_PUSHES.clear()


def _card(i):
    return cards.Card(card_id=f"{i:010x}", user_id="owner1", kind=cards.KIND_PROPOSAL,
                      origin=cards.ORIGIN_AGENT, title=f"Proposed: /send {i}",
                      verb="/send", args=[str(i), "USDC", "to", "0x" + "ab" * 20])


async def _burst(n, start=0):
    outs = [await cards.push(None, "owner1", _card(start + i), session_id="s") for i in range(n)]
    for _ in range(5):
        await asyncio.sleep(0)
    return outs


@pytest.mark.asyncio
async def test_held_pushes_collapse_into_one_line(rail):
    outs = await _burst(3)
    assert outs == ["rate_limited"] * 3
    lines = [(t, kw) for t, kw in rail if not kw.get("card_id")]
    assert len(lines) == 1
    text, kw = lines[0]
    assert "3 more cards wait in /cards" in text
    assert kw["priority"] == "exempt"


@pytest.mark.asyncio
async def test_one_line_per_window(rail):
    await _burst(2)
    await _burst(2, start=10)
    assert len([t for t, kw in rail if not kw.get("card_id")]) == 1


@pytest.mark.asyncio
async def test_a_delivered_push_adds_no_line(monkeypatch):
    sent = []

    async def _deliver(container, user_id, text, **kw):
        sent.append(text)
        return "sent"
    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)
    cards._HELD_PUSHES.clear()
    assert await cards.push(None, "owner1", _card(1)) == "sent"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_pushes_held_inside_the_window_are_announced_when_it_ends(rail, monkeypatch):
    """A push held during the window after a line was counted and never said unless
    another push came after the window: the owner never heard of those cards."""
    monkeypatch.setattr(cards, "HELD_PUSH_WINDOW_S", 0.05)
    await _burst(1)
    await _burst(2, start=10)
    assert len([t for t, kw in rail if not kw.get("card_id")]) == 1
    await asyncio.sleep(0.15)
    lines = [t for t, kw in rail if not kw.get("card_id")]
    assert len(lines) == 2 and "2 more cards wait in /cards" in lines[1]
