"""109 W3: the public-thread intake — code answers /buy and /order in the thread."""
import copy

import pytest

from core.commerce import hooks
from core.commerce import orders as ords
from core.commerce import store as st
from core.commerce import thread
from core.commerce.thread import PublicPost, ThreadUnavailable
from modules.commerce import service
from modules.commerce import storefront as sf
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST
from tests.unit.modules.commerce.test_order_service_e2e import _db, _env  # noqa: F401

ADDR = "0x" + "ab" * 20


class Chan:
    surface = "x"

    def __init__(self):
        self.posts, self.replies, self.budget, self.fail = [], [], 99, False
        self.down = None

    async def poll(self, since_id):
        if self.down:
            raise ThreadUnavailable(self.down)
        new = [p for p in self.posts if since_id is None or int(p.post_id) > int(since_id)]
        return new, (new[-1].post_id if new else since_id)

    async def lookup(self, post_id):
        return None

    def agent_id(self):
        return "me"

    def can_reply(self):
        return self.budget > 0

    async def reply(self, post_id, text, *, subject="", user_id=""):
        self.users = getattr(self, "users", []) + [user_id]
        if self.fail:
            return False
        self.budget -= 1
        self.replies.append((post_id, text))
        return True


@pytest.fixture(autouse=True)
def chan():
    sf._RATE.clear()
    service._LAST_TRY.clear()
    c = Chan()
    hooks.register_thread_channel("x", c)
    yield c
    hooks.unregister_thread_channel("x")
    sf._RATE.clear()


def _offer(rail="usdg-robinhood"):
    m = copy.deepcopy(MANIFEST)
    m["listings"] = ["native:x"]
    m["rails"] = [{"asset": rail, "mode": "transfer"}]
    m["pricing"] = {"kind": "fixed", "amount_usd": "3.30"}
    o = st.save_draft("owner", m)
    o = st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
    thread.announce(o.id, "x", "100", now=1.0)
    return o


def P(pid, author, text, root="100", **kw):
    return PublicPost("x", str(pid), author, text, root_id=root, reply_to_id=root,
                      created_at=kw.pop("created_at", 10.0), **kw)


@pytest.mark.asyncio
async def test_a_reply_gets_its_payment_answer_in_the_thread(tmp_path, chan):
    db = await _db(tmp_path)
    try:
        _offer()
        chan.posts = [P(101, "42", f"@agent /buy custom-art delivery_address={ADDR} brief=red cat")]
        assert await sf.thread_intake(None, "x", db=db) == 1
        (pid, text), = chan.replies
        o = ords.by_thread("x", "101")
        assert pid == "101" and o.buyer == "42" and o.inputs["delivery_address"] == ADDR
        assert text.startswith(f"Order {o.id}: pay exactly 3.3") and "USDG" in text
        assert "robinhood" in text and "0x" + "77" * 20 in text.lower()
        assert "reply from this account" in text
        assert o.source == "native:x:post:101"
        # a re-read of the same post (cursor lost) makes no second order and no reply
        thread.save_cursor("x", "50")
        await sf.thread_intake(None, "x", db=db)
        assert len(chan.replies) == 1 and len(ords.list_orders(channel="x")) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_what_the_intake_ignores(tmp_path, chan):
    db = await _db(tmp_path)
    try:
        _offer()
        line = f"/buy delivery_address={ADDR} brief=x"
        chan.posts = [
            P(101, "42", line, root="999"),               # under another root
            P(102, "42", line, edited=True),              # an edited post
            P(103, "me", line),                           # the agent itself
            P(104, "42", line, created_at=0.5),           # older than the announcement
            P(105, "42", "how much is it?"),              # not a storefront line: no answer
            P(106, "42", "/offers"),                      # the announcement is the catalog
        ]
        assert await sf.thread_intake(None, "x", db=db) == 0
        assert chan.replies == [] and ords.list_orders(channel="x") == []
        assert thread.cursor("x")["since_id"] == "106"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_only_the_buyer_reads_or_cancels_the_order(tmp_path, chan):
    db = await _db(tmp_path)
    try:
        _offer()
        chan.posts = [P(101, "42", f"/buy delivery_address={ADDR} brief=x")]
        await sf.thread_intake(None, "x", db=db)
        oid = ords.by_thread("x", "101").id
        chan.posts.append(P(102, "666", f"/order {oid} cancel"))
        chan.posts.append(P(103, "42", f"/order {oid}"))
        await sf.thread_intake(None, "x", db=db)
        assert chan.replies[1] == ("102", "No such order of yours.")
        assert "awaiting_payment" in chan.replies[2][1]
        assert ords.get(oid).status == ords.AWAITING
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_bad_line_is_refused_once(tmp_path, chan):
    db = await _db(tmp_path)
    try:
        _offer()
        chan.posts = [P(101, "42", "/buy delivery_address=nope")]
        await sf.thread_intake(None, "x", db=db)
        await sf.thread_intake(None, "x", db=db)
        assert len(chan.replies) == 1 and chan.replies[0][1].startswith("❌")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_write_share_holds_the_post_for_the_next_poll(tmp_path, chan):
    db = await _db(tmp_path)
    try:
        _offer()
        chan.budget = 0
        chan.posts = [P(101, "42", f"/buy delivery_address={ADDR} brief=x")]
        assert await sf.thread_intake(None, "x", db=db) == 0
        c = thread.cursor("x")
        assert c["since_id"] is None and "write share" in c["stop_reason"]
        assert ords.list_orders(channel="x") == []
        chan.budget = 5
        assert await sf.thread_intake(None, "x", db=db) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_platform_refusal_is_the_stop_reason_and_nothing_moves(tmp_path, chan):
    _offer()
    chan.down = "x api: 402 (no credit)"
    assert await sf.thread_intake(None, "x") == 0
    assert thread.cursor("x")["stop_reason"] == "x api: 402 (no credit)"


@pytest.mark.asyncio
async def test_closed_without_an_announcement(tmp_path, chan):
    m = copy.deepcopy(MANIFEST)
    m["listings"] = ["native:x"]
    o = st.save_draft("owner", m)
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
    chan.posts = [P(101, "42", f"/buy delivery_address={ADDR} brief=x")]
    assert await sf.thread_intake(None, "x") == 0
    assert thread.cursor("x") is None             # no platform read at all


@pytest.mark.asyncio
async def test_notices_follow_the_thread_and_are_retried(tmp_path, chan):
    db = await _db(tmp_path)
    try:
        _offer()
        chan.posts = [P(101, "42", f"/buy delivery_address={ADDR} brief=x")]
        await sf.thread_intake(None, "x", db=db)
        o = ords.by_thread("x", "101")
        chan.fail = True
        assert await service.tell_buyer(None, o, "Payment received") is False
        assert ords.get(o.id).notice_pending == "Payment received"
        chan.fail = False
        out = await service.tick(now=ords.get(o.id).updated_at + 10)
        assert out["notices"] == 1 and chan.replies[-1] == ("101", "Payment received")
        assert ords.get(o.id).notice_pending is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_unsendable_notice_reaches_the_owner_once(tmp_path, chan, monkeypatch):
    told = []

    async def _owner(container, uid, text):
        told.append(text)
    monkeypatch.setattr(service, "owner_notice", _owner)
    o = ords.create(offer_id="a", offer_version=1, user_id="owner", channel="x", buyer="42",
                    inputs={}, amount_usd="1", spend_budget=0, thread_ref="101")
    chan.fail = True
    await service.tell_buyer(None, o, "Order delivered")
    await service.tick(now=o.updated_at + service.NOTICE_GIVE_UP_S + 1)
    await service.tick(now=o.updated_at + service.NOTICE_GIVE_UP_S + 2)
    assert len(told) == 1 and "could not be told" in told[0]


def test_thread_line_strips_mentions_and_reads_only_two_verbs():
    assert sf.thread_line("@a @b /buy x=1") == ("/buy", ["x=1"])
    assert sf.thread_line("/ORDER ord_1") == ("/order", ["ord_1"])
    assert sf.thread_line("@a please /buy") is None
    assert sf.thread_line("/offers") is None


@pytest.mark.asyncio
async def test_one_failing_post_never_blocks_the_ones_behind_it(tmp_path, chan, monkeypatch):
    db = await _db(tmp_path)
    try:
        _offer()
        real = sf._order

        async def _boom(surface, buyer, words, container=None, db=None):
            if buyer == "13":
                raise RuntimeError("boom")
            return await real(surface, buyer, words, container=container, db=db)
        monkeypatch.setattr(sf, "_order", _boom)
        chan.posts = [P(101, "13", "/order ord_x"), P(102, "42", "/order")]
        await sf.thread_intake(None, "x", db=db)
        assert [r[0] for r in chan.replies] == ["102"]
        assert thread.cursor("x")["since_id"] == "102"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ignored_posts_do_not_use_the_per_poll_cap(tmp_path, chan):
    db = await _db(tmp_path)
    try:
        _offer()
        chan.posts = [P(100 + i, str(i), "spam") for i in range(1, 30)]
        chan.posts.append(P(200, "42", "/order"))
        assert await sf.thread_intake(None, "x", db=db) == 1
        assert chan.replies[-1][0] == "200"
    finally:
        await db.close()
