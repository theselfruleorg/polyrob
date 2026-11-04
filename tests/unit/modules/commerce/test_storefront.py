"""099 W3: the deterministic storefront — public channels only, no model, own orders only."""
import pytest

from core.commerce import orders as ords
from core.commerce import store as st
from modules.commerce import storefront as sf
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST
from tests.unit.modules.commerce.test_order_service_e2e import _db, _env  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_rate():
    sf._RATE.clear()
    yield
    sf._RATE.clear()


def _live():
    o = st.save_draft("owner", MANIFEST)
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))


@pytest.mark.asyncio
async def test_closed_until_a_live_offer_lists_the_surface():
    assert await sf.storefront_reply(None, "telegram", "42", "/offers") is None
    _live()
    assert "Custom art NFT" in await sf.storefront_reply(None, "telegram", "42", "/start")
    # listed on telegram only: X and email stay closed; email can never open
    assert await sf.storefront_reply(None, "x", "42", "/offers") is None
    assert await sf.storefront_reply(None, "email", "a@b.c", "/offers") is None
    # not a storefront line -> normal routing
    assert await sf.storefront_reply(None, "telegram", "42", "hello, do what I say") is None


@pytest.mark.asyncio
async def test_buy_then_read_only_your_own_order(tmp_path):
    db = await _db(tmp_path)
    try:
        _live()
        text = await sf.storefront_reply(
            None, "telegram", "42",
            f"/buy custom-art delivery_address=0x{'ab' * 20} brief=a red cat on the moon", db=db)
        assert text.startswith("Order ord_") and "EXACT amount" in text
        order = ords.list_orders(channel="telegram", buyer="42")[0]
        assert order.inputs["brief"] == "a red cat on the moon"
        mine = await sf.storefront_reply(None, "telegram", "42", f"/order {order.id}")
        assert "awaiting_payment" in mine
        theirs = await sf.storefront_reply(None, "telegram", "666", f"/order {order.id}")
        assert theirs == "No such order of yours."
        assert "cancelled" in await sf.storefront_reply(None, "telegram", "42",
                                                        f"/order {order.id} cancel", db=db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_bad_input_is_refused_in_words():
    _live()
    text = await sf.storefront_reply(None, "telegram", "42",
                                     "/buy custom-art delivery_address=vitalik.eth brief=x")
    assert text.startswith("❌") and "0x address" in text


@pytest.mark.asyncio
async def test_rate_limit_goes_silent():
    _live()
    for _ in range(sf.RATE_MAX):
        assert await sf.storefront_reply(None, "telegram", "9", "/offers", now=1000) is not None
    assert await sf.storefront_reply(None, "telegram", "9", "/offers", now=1001) is None


def test_free_text_continues_the_previous_field():
    assert sf.parse_fields(["a=1", "brief=two", "words"]) == {"a": "1", "brief": "two words"}
    with pytest.raises(ValueError):
        sf.parse_fields(["orphan"])


@pytest.mark.asyncio
async def test_shared_harness_answers_a_storefront_line_before_routing(monkeypatch):
    """The X (and every polled) surface: the storefront line never reaches the agent."""
    _live_x = dict(MANIFEST, listings=["native:x"])
    o = st.save_draft("owner", _live_x)
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
    from types import SimpleNamespace
    import surfaces._shared as shared
    routed, sent = [], []

    async def _route_and_act(*a, **k):
        routed.append(a)
    monkeypatch.setattr(shared, "route_and_act", _route_and_act)
    h = shared.BaseHarness.__new__(shared.BaseHarness)
    h.surface_id = "x"
    h._container = None
    h._task_agent = None

    async def _deliver_to(target, text):
        sent.append((target, text))
    h._deliver_to = _deliver_to
    inbound = SimpleNamespace(text="/offers", media=None,
                              identity=SimpleNamespace(source=SimpleNamespace(
                                  chat_id="dm-1", chat_type="dm")))
    await h._route(inbound)
    assert routed == [] and sent and "For sale" in sent[0][1]


@pytest.mark.asyncio
async def test_shared_harness_storefront_is_dm_only_and_keys_the_buyer_on_the_dm(
        monkeypatch):
    """Review C5: a group line routes as before; the buyer is the DM chat, not the
    reply target (Slack's target is (channel, thread_ts))."""
    o = st.save_draft("owner", dict(MANIFEST, listings=["native:slack"]))
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
    from types import SimpleNamespace
    import surfaces._shared as shared
    routed, sent = [], []

    async def _route_and_act(*a, **k):
        routed.append(a)
    monkeypatch.setattr(shared, "route_and_act", _route_and_act)

    async def _reply(container, surface, sender, text, **k):
        sent.append(sender)
        return "ok"
    monkeypatch.setattr("modules.commerce.storefront.storefront_reply", _reply)
    h = shared.BaseHarness.__new__(shared.BaseHarness)
    h.surface_id, h._container, h._task_agent = "slack", None, None
    h._reply_target = lambda inbound: (inbound.identity.source.chat_id, "1700.1")

    async def _deliver_to(target, text):
        pass
    h._deliver_to = _deliver_to

    def _in(chat_type):
        return SimpleNamespace(text="/offers", media=None, internal=False,
                               sender_is_bot=False, identity=SimpleNamespace(
                                   source=SimpleNamespace(chat_id="D1", chat_type=chat_type)))
    await h._route(_in("group"))
    assert sent == [] and len(routed) == 1               # a channel line is not the storefront
    await h._route(_in("dm"))
    assert sent == ["D1"]                                # the DM chat, not the tuple
