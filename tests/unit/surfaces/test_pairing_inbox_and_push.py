"""113 W6 C-12 / F-16: a pairing request is in the one Inbox and pushed once.

A stranger who asked to talk to the agent waits for the owner. Before, only
``/pair`` showed it and nothing told the owner it was there. Now the Inbox has
a ``pairing`` source (owner only, a read never creates the store, an unreadable
store is named) and the owner gets ONE push when a new sender first asks. The
card and the push carry no code: the owner approves the code the person sends.
"""
import asyncio

import pytest

from core import pairing


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_REQUIRE_PAIRING", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    pairing._STORES.clear()
    yield str(tmp_path)
    pairing._STORES.clear()


def _ask(home, uid="telegram:42", raw="42"):
    store = pairing.store_for(home)
    return pairing.evaluate_access(uid, store=store, required=True,
                                   surface="telegram", raw_id=raw)


def test_a_new_sender_is_a_new_request_once(home):
    first = _ask(home)
    assert not first.allowed and first.pairing_code and first.new_request
    again = _ask(home)
    assert not again.new_request


def test_the_inbox_lists_a_waiting_request_for_the_owner(home):
    from surfaces.inbox_sources import build_inbox, collect_pairing
    _ask(home)
    items = collect_pairing("u_owner", data_dir=home)
    assert [i.kind for i in items] == ["pairing"]
    code = pairing.store_for(home).list_all()[0]["code"]
    assert code not in items[0].body and code not in items[0].title
    assert items[0].extra["surface"] == "telegram" and items[0].expires_at
    assert collect_pairing("u_other", data_dir=home) == []
    body = build_inbox("u_owner", data_dir=home, instance_id="i1")
    assert any(i.get("kind") == "pairing" for i in body["items"])


def test_the_remedy_names_the_pair_verb_for_that_surface(home):
    from core.surfaces.inbox_render import CHAT_REMEDIES, REPL_REMEDIES, _remedy_line
    item = {"kind": "pairing", "id": "telegram:42", "blocking": True,
            "extra": {"surface": "telegram"}}
    assert "/pair approve telegram <their code>" in _remedy_line(item, REPL_REMEDIES)
    assert "/pair approve telegram <their code>" in _remedy_line(item, CHAT_REMEDIES)


def test_a_read_never_creates_the_store_and_pairing_off_lists_nothing(home, monkeypatch, tmp_path):
    from surfaces.inbox_sources import collect_pairing
    empty = tmp_path / "empty"
    empty.mkdir()
    assert collect_pairing("u_owner", data_dir=str(empty)) == []
    assert not (empty / "pairing.db").exists()
    _ask(home)
    monkeypatch.setenv("POLYROB_REQUIRE_PAIRING", "false")
    assert collect_pairing("u_owner", data_dir=home) == []


def test_an_unreadable_store_is_named_not_empty(home):
    from pathlib import Path
    from surfaces.inbox_sources import build_inbox
    Path(home, "pairing.db").write_text("not a database")
    body = build_inbox("u_owner", data_dir=home, instance_id="i1")
    assert any(i.get("unreadable") and i.get("source") == "pairing" for i in body["items"])


def test_the_owner_is_pushed_once_on_a_new_request(home, monkeypatch):
    sent = []

    async def fake_push(container, user_id, text, **kw):
        sent.append((text, kw.get("source")))
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", fake_push)
    container = type("C", (), {"config": type("Cfg", (), {"data_dir": home})()})()

    async def run():
        for _ in range(3):
            pairing.guard_inbound(container, "telegram:42", surface_id="telegram", raw_id="42")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert len(sent) == 1
    text, source = sent[0]
    assert source == "pairing" and "/pair approve telegram <code>" in text
    assert pairing.store_for(home).list_all()[0]["code"] not in text
