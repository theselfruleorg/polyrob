"""109 W0: the storefront seams that had no test."""
from types import SimpleNamespace

import pytest

from core.commerce import orders as ords
from modules.commerce import service, storefront as sf


def _inbound(chat_type="dm", chat_id="7", **kw):
    src = SimpleNamespace(chat_type=chat_type, chat_id=chat_id)
    return SimpleNamespace(identity=SimpleNamespace(source=src), **kw)


def test_storefront_buyer_is_a_persons_dm_only():
    from surfaces._shared import _storefront_buyer
    assert _storefront_buyer(_inbound()) == "7"
    assert _storefront_buyer(_inbound(chat_type="group")) == ""
    assert _storefront_buyer(_inbound(internal=True)) == ""
    assert _storefront_buyer(_inbound(sender_is_bot=True)) == ""
    assert _storefront_buyer(SimpleNamespace()) == ""


def test_native_surfaces_never_hold_a_forgeable_surface():
    from core.commerce.offer import native_surfaces
    from core.surfaces import catalog
    got = native_surfaces()
    assert "email" not in got and "telegram" in got
    assert not set(got) & set(catalog.forgeable_ids())


class _Router:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, surface_id="telegram", media=None):
        self.sent.append((chat_id, surface_id, text))
        return True


def _order(**kw):
    base = dict(id="ord_1", offer_id="a", offer_version=1, user_id="o", status="paid",
                channel="telegram", buyer="42")
    base.update(kw)
    return ords.Order(**base)


@pytest.mark.asyncio
async def test_a_dm_buyer_is_pushed_on_its_own_surface_and_a_machine_buyer_is_not():
    r = _Router()
    c = SimpleNamespace(get_service=lambda name: r if name == "message_router" else None)
    assert await service.tell_buyer(c, _order(), "hi") is True
    assert r.sent == [("42", "telegram", "hi")]
    assert await service.tell_buyer(c, _order(channel="a2a"), "hi") is False
    assert await service.tell_buyer(None, _order(), "hi") is False


@pytest.mark.asyncio
async def test_the_rate_window_expires_and_counts_per_sender(monkeypatch):
    sf._RATE.clear()
    monkeypatch.setattr(sf, "is_open", lambda s: True)
    monkeypatch.setattr(sf, "catalog_text", lambda s: "c")
    for _ in range(sf.RATE_MAX):
        await sf.storefront_reply(None, "telegram", "9", "/offers", now=1000)
    assert await sf.storefront_reply(None, "telegram", "9", "/offers", now=1001) is None
    assert await sf.storefront_reply(None, "telegram", "8", "/offers", now=1001) == "c"
    assert await sf.storefront_reply(None, "telegram", "9", "/offers",
                                     now=1000 + sf.RATE_WINDOW_S + 1) == "c"
    sf._RATE.clear()
