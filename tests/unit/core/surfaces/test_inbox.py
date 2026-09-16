"""043 C5 — the Inbox composition, held to its own definition.

An Inbox item is *a durable record, for this tenant, blocked on an owner
decision, that Rob cannot resolve alone* (043 §1.3). All four tests must hold,
and the two that are easiest to lose are the last two: an informational card is
**listed and not counted**, and a source that could not be read is a **named
entry**, never an omission and never a zero.

The composition lives in ``core/surfaces/inbox.py`` and takes its collectors as
an argument, so the REPL (``/inbox``) and the console render the same list
without either importing the other.
"""
import pytest

from core.surfaces.inbox import (
    SOURCE_LABELS,
    Item,
    build_inbox,
    compose,
)


def _item(**kw):
    base = dict(kind="ask", id="1", title="x", blocking=True,
                expires_at=None, created_at=1.0)
    base.update(kw)
    return Item(**base)


# --- what counts ------------------------------------------------------------ #

def test_the_badge_counts_decisions_not_information():
    out = compose([_item(id="1", blocking=True),
                   _item(kind="invoice", id="2", blocking=False,
                         created_at=0.5, title="late")], sources={})
    assert out["count"] == 1
    assert [i["id"] for i in out["not_blocking"]] == ["2"]
    assert [i["id"] for i in out["items"]] == ["1"]


def test_an_unreadable_entry_is_listed_and_never_counted():
    out = compose([_item(id="1"),
                   Item(kind="unreadable", id="asks", title="blocked goals",
                        blocking=True, unreadable=True)], sources={})
    assert out["count"] == 1
    assert out["uncertain"] is True
    assert [i["id"] for i in out["items"]] == ["1", "asks"]


def test_nothing_waiting_is_only_sayable_when_every_source_answered():
    clean = compose([], sources={"asks": "ok", "apps": "ok"})
    assert clean["count"] == 0 and clean["uncertain"] is False
    assert clean["unreadable_sources"] == []

    refused = compose([], sources={"asks": "ok", "apps": "unreadable(locked)"})
    assert refused["count"] == 0 and refused["uncertain"] is True
    assert refused["unreadable_sources"] == ["apps"]


# --- the order -------------------------------------------------------------- #

def test_order_is_blocking_then_expiring_soonest_then_oldest():
    items = [
        _item(id="old", created_at=1.0),
        _item(id="new", created_at=9.0),
        _item(id="expires_late", expires_at=500.0, created_at=9.0),
        _item(id="expires_soon", expires_at=100.0, created_at=9.0),
        _item(id="info", blocking=False, created_at=0.0),
    ]
    out = compose(items, sources={})
    assert [i["id"] for i in out["items"]] == [
        "expires_soon", "expires_late", "old", "new"]
    assert [i["id"] for i in out["not_blocking"]] == ["info"]


def test_an_unreadable_entry_sorts_after_the_dated_decisions():
    items = [Item(kind="unreadable", id="apps", title="apps",
                  blocking=True, unreadable=True),
             _item(id="real", created_at=5.0)]
    out = compose(items, sources={})
    assert [i["id"] for i in out["items"]] == ["real", "apps"]


# --- build_inbox: a collector that refuses is NAMED ------------------------- #

def test_a_refusing_collector_becomes_a_named_source_and_an_entry():
    def boom(_uid):
        raise RuntimeError("goals.db locked")

    body = build_inbox("u1", {"asks": boom, "apps": lambda _u: []})
    assert body["uncertain"] is True
    assert body["count"] == 0
    assert body["sources"]["asks"].startswith("unreadable(")
    assert "goals.db locked" in body["sources"]["asks"]
    assert body["sources"]["apps"] == "ok"
    assert [i["id"] for i in body["items"]] == ["asks"]
    assert body["items"][0]["unreadable"] is True


def test_a_collector_that_answers_contributes_its_items():
    body = build_inbox("u1", {"asks": lambda _u: [_item(id="a1")]})
    assert body["count"] == 1
    assert body["sources"] == {"asks": "ok"}
    assert body["uncertain"] is False


def test_a_collector_returning_none_is_ok_not_unreadable():
    body = build_inbox("u1", {"apps": lambda _u: None})
    assert body["sources"] == {"apps": "ok"}
    assert body["count"] == 0 and body["uncertain"] is False


def test_every_source_name_has_a_label_a_person_can_read():
    body = build_inbox("u1", {name: (lambda _u: []) for name in SOURCE_LABELS})
    assert set(body["sources"]) == set(SOURCE_LABELS)
    for label in SOURCE_LABELS.values():
        assert label and label == label.strip()
        assert label.islower() or label[0].islower()


def test_the_unreadable_entry_names_the_source_in_words():
    def boom(_uid):
        raise OSError("disk")

    body = build_inbox("u1", {"tool_approvals": boom})
    assert body["items"][0]["title"] == SOURCE_LABELS["tool_approvals"]


def test_a_collector_may_not_take_the_whole_inbox_down():
    """One store refusing must never lose the items the others returned."""
    def boom(_uid):
        raise RuntimeError("nope")

    body = build_inbox("u1", {"asks": boom, "apps": lambda _u: [_item(id="ok1")]})
    assert [i["id"] for i in body["items"]] == ["ok1", "asks"]
    assert body["count"] == 1


# --- the item shape --------------------------------------------------------- #

def test_an_item_serializes_every_field_a_seat_renders():
    out = compose([_item(id="1", body="why", meta="waited",
                         actions=("approve", "reject"), source="asks")],
                  sources={})
    row = out["items"][0]
    for key in ("kind", "id", "title", "body", "meta", "blocking",
                "expires_at", "created_at", "actions", "source", "unreadable"):
        assert key in row
    assert row["actions"] == ["approve", "reject"]


def test_an_action_outside_the_vocabulary_is_refused():
    with pytest.raises(ValueError):
        compose([_item(actions=("delete_everything",))], sources={})


# --- the card a person reads ------------------------------------------------- #

def test_a_correspondent_card_is_titled_as_a_sentence_not_an_id(monkeypatch):
    """⚠️ ``telegram:12345`` is ROUTING grammar. It says nothing about what is
    being decided, and it was the first line of a card whose whole job is to be
    decided. The address stays in the body, where it identifies who."""
    import surfaces.inbox_sources as src

    class Registry:
        def list(self, user_id=None):
            return [{"surface": "telegram", "address": "12345", "thread_id": "",
                     "session_id": "s1", "user_id": user_id, "state": "pending"}]

    monkeypatch.setattr(src, "_store", lambda path: path)
    monkeypatch.setattr("core.surfaces.correspondents.CorrespondentRegistry",
                        lambda path: Registry())
    items = src.collect_correspondents("u1", data_dir="/tmp/nope")
    assert len(items) == 1
    assert items[0].title == "telegram contact awaiting approval"
    assert items[0].id == "telegram:12345"        # routing is preserved
    assert "12345" in items[0].body               # …and still visible


def test_a_correspondent_with_no_surface_still_reads_as_a_sentence(monkeypatch):
    import surfaces.inbox_sources as src

    class Registry:
        def list(self, user_id=None):
            return []

    monkeypatch.setattr(src, "_store", lambda path: path)
    monkeypatch.setattr("core.surfaces.correspondents.CorrespondentRegistry",
                        lambda path: Registry())
    monkeypatch.setattr("core.surfaces.owner_admin.pending_correspondent_items",
                        lambda registry, tenant: [{"id": "weird", "preview": "x"}])
    items = src.collect_correspondents("u1", data_dir="/tmp/nope")
    assert items[0].title == "A contact awaiting approval"
