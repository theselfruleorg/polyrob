"""109 W2: the public-thread contract — tables, one order per post, the hook, announce."""
import copy
import sqlite3
from types import SimpleNamespace

import pytest

from core.commerce import hooks
from core.commerce import orders as ords
from core.commerce import store as st
from core.commerce import thread
from core.commerce.thread import PublicPost
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST


@pytest.fixture
def db(monkeypatch, tmp_path):
    p = str(tmp_path / "commerce.db")
    monkeypatch.setattr(st, "db_path", lambda data_dir=None: p)
    return p


def _live(listings=("native:x", "a2a")):
    m = copy.deepcopy(MANIFEST)
    m["listings"] = list(listings)
    o = st.save_draft("owner", m)
    return st.go_live(o.id, "owner", actor=st.OWNER_SEAT,
                      expect_hash=st.manifest_hash(o.manifest))


def test_an_old_store_gains_the_thread_columns_and_index(tmp_path):
    p = str(tmp_path / "old.db")
    con = sqlite3.connect(p)
    old = ords.ORDER_SCHEMA.replace("    delivered_at   REAL\n", "    delivered_at   REAL\n")
    con.executescript(old)
    con.execute("ALTER TABLE offer_orders DROP COLUMN payment_tx")
    con.commit()
    con.close()
    st.init_schema(p)
    cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(offer_orders)")}
    assert {"thread_ref", "notice_pending", "payment_tx"} <= cols


def test_one_order_per_post(db):
    kw = dict(offer_id="custom-art", offer_version=1, user_id="owner", channel="x",
              buyer="42", inputs={}, amount_usd="3.30", spend_budget=0.2)
    o = ords.create(thread_ref="p1", **kw)
    assert ords.by_thread("x", "p1").id == o.id
    with pytest.raises(ords.OrderError, match="already made an order"):
        ords.create(thread_ref="p1", **kw)
    ords.create(thread_ref="p2", **kw)
    ords.create(**kw)                 # a DM order has no thread and never collides
    ords.create(**kw)


def test_pending_notice_is_held_and_cleared(db):
    o = ords.create(offer_id="a", offer_version=1, user_id="owner", channel="x", buyer="1",
                    inputs={}, amount_usd="1", spend_budget=0, thread_ref="p9")
    ords.set_notice_pending(o.id, "Order paid")
    assert [x.id for x in ords.pending_notices()] == [o.id]
    assert ords.get(o.id).notice_pending == "Order paid"
    ords.set_notice_pending(o.id, None)
    assert ords.pending_notices() == []


def test_a_root_announces_one_offer_and_the_cursor_keeps_its_place(db):
    thread.announce("a", "x", "r1")
    thread.announce("a", "x", "r1")                  # idempotent
    assert thread.offer_of("x", "r1") == "a"
    with pytest.raises(ValueError, match="already announces"):
        thread.announce("b", "x", "r1")
    thread.save_cursor("x", "100")
    thread.save_cursor("x", None, stop_reason="x api: 402")
    c = thread.cursor("x")
    assert c["since_id"] == "100" and c["stop_reason"] == "x api: 402"


def test_the_loader_hook_takes_only_a_surface_the_pack_declares(monkeypatch):
    from core.packs import loader
    rec = SimpleNamespace(manifest=SimpleNamespace(surfaces=(SimpleNamespace(id="x"),)))
    made = object()
    monkeypatch.setattr(loader, "resolve_ref", lambda ref: (lambda: made))
    loader.HOOKS["commerce.thread_channel"](rec, {"x": "m:f"})
    assert hooks.thread_channel("x") is made
    with pytest.raises(ValueError, match="declares no such surface"):
        loader.HOOKS["commerce.thread_channel"](rec, {"telegram": "m:f"})
    hooks.unregister_thread_channel("x")


class _Chan:
    surface = "x"

    def __init__(self, posts):
        self.posts = posts

    async def lookup(self, post_id):
        return self.posts.get(post_id)

    def agent_id(self):
        return "me"


@pytest.mark.asyncio
async def test_announce_needs_a_live_listed_offer_and_the_agents_own_root(db, monkeypatch):
    from tools.controller.offer_actions import announce_post
    o = _live()
    hooks.register_thread_channel("x", _Chan({
        "mine": PublicPost("x", "mine", "me", "Buy one", root_id="mine"),
        "theirs": PublicPost("x", "theirs", "99", "fake", root_id="theirs"),
        "reply": PublicPost("x", "reply", "me", "re", root_id="mine", reply_to_id="mine")}))
    try:
        for pid, needle in (("theirs", "not this agent's"), ("reply", "is a reply"),
                            ("gone", "could not be read")):
            with pytest.raises(st.CommerceError, match=needle):
                await announce_post(o.id, "owner", "x", pid)
        with pytest.raises(st.CommerceError, match="no offer"):
            await announce_post(o.id, "intruder", "x", "mine")
        assert "announces" in await announce_post(o.id, "owner", "x", "mine")
        assert thread.offer_of("x", "mine") == o.id
    finally:
        hooks.unregister_thread_channel("x")


@pytest.mark.asyncio
async def test_announce_refuses_an_offer_not_listed_on_the_surface(db):
    from tools.controller.offer_actions import announce_post
    o = _live(listings=("a2a",))
    with pytest.raises(st.CommerceError, match="native:x"):
        await announce_post(o.id, "owner", "x", "mine")
