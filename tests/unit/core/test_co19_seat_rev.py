"""098 close-out CO-19: every owner seat sends the draft revision it showed.

A decide tap carries the rev (``/approve_p_<alias>_<rev>``), the console POST
carries it, the CLI/REPL decide ``<id>@<rev>``. A decision with NO rev binds to
the card the owner was sent; a draft whose card was never sent is REFUSED, never
decided on whatever it holds now.
"""
from __future__ import annotations

import pytest

from core import self_evolution as se
from core.instance import DEFAULT_INSTANCE_ID
from core.self_context_writer import (
    DRAFT_CHANGED, PROVENANCE_BACKGROUND, SelfContextWriter,
)
from core.surfaces.tappable import parse_tappable


@pytest.fixture(autouse=True)
def _clean_scan(monkeypatch):
    import modules.memory.task.threat_scan as ts
    monkeypatch.setattr(ts, "is_identity_suspicious", lambda body: False)


def _draft(home, text="- prefers short replies\n", uid="alice"):
    w = SelfContextWriter(home, instance_id=DEFAULT_INSTANCE_ID)
    assert w.propose(text, user_id=uid, created_by=PROVENANCE_BACKGROUND).pending
    return w


def _items(home, uid="alice"):
    return se.list_pending(uid, home_dir=home, instance_id=DEFAULT_INSTANCE_ID)


def _patch(w, uid="alice"):
    w.patch(user_id=uid, old_string="short", new_string="long",
            created_by=PROVENANCE_BACKGROUND)


def test_a_decision_with_no_rev_on_a_never_shown_draft_is_refused(tmp_path):
    w = _draft(tmp_path)
    for fn in (se.promote, se.reject):
        ok, msg = fn("self_context", "alice", user_id="alice", home_dir=tmp_path,
                     instance_id=DEFAULT_INSTANCE_ID)
        assert not ok and msg == se.DRAFT_NOT_SHOWN
    assert w.read_active("alice") == "" and w.list_pending("alice") is not None


def test_a_sent_card_still_binds_a_decision_with_no_rev(tmp_path):
    w = _draft(tmp_path)
    se.save_notified_state("alice", se.pending_notify_state(_items(tmp_path)),
                           home_dir=tmp_path, instance_id=DEFAULT_INSTANCE_ID)
    ok, msg = se.promote("self_context", "alice", user_id="alice", home_dir=tmp_path,
                         instance_id=DEFAULT_INSTANCE_ID)
    assert ok, msg
    assert "short replies" in w.read_active("alice")


def test_a_decide_token_carries_the_rev_and_a_read_token_does_not(tmp_path):
    _draft(tmp_path)
    [item] = _items(tmp_path)
    tok = se.pending_tap_token("approve", item)
    alias = se.pending_tap_alias("self_context", "alice").replace("-", "_")
    assert tok == f"/approve_{alias}_{item['rev'][:se.TAP_REV_LEN]}"
    assert len(tok) <= 32  # a chat client links the whole token
    assert se.pending_tap_token("pending", item) == f"/pending_{alias}"
    verb, arg = parse_tappable(tok)
    assert verb == "/approve"
    hit = se.resolve_pending_alias(arg, _items(tmp_path))
    assert hit["id"] == "alice" and hit["seat_rev"] == item["rev"]


def test_a_tap_on_a_changed_draft_is_refused_and_the_old_alias_still_resolves(tmp_path):
    w = _draft(tmp_path)
    [item] = _items(tmp_path)
    _, arg = parse_tappable(se.pending_tap_token("approve", item))
    _patch(w)
    hit = se.resolve_pending_alias(arg, _items(tmp_path))
    assert hit is not None and hit["seat_rev"] == item["rev"][:se.TAP_REV_LEN]
    ok, msg = se.promote("self_context", hit["id"], rev=hit["seat_rev"], user_id="alice",
                         home_dir=tmp_path, instance_id=DEFAULT_INSTANCE_ID)
    assert not ok and DRAFT_CHANGED in msg
    assert w.read_active("alice") == ""


def test_typed_id_at_rev_and_the_seat_lines(tmp_path):
    from core import pending_seats
    _draft(tmp_path)
    [item] = _items(tmp_path)
    assert se.decision_id(item) == f"alice@{item['rev']}"
    assert se.split_id_rev(f"alice@{item['rev']}") == ("alice", item["rev"])
    assert se.split_id_rev("email:a@x.io") == ("email:a@x.io", None)
    line = pending_seats.decide_line(item, pending_seats.SEAT_CLI)
    assert f"polyrob owner promote self_context alice@{item['rev']}" in line


def test_decided_token_annotation_reads_a_rev_token(tmp_path):
    item = {"kind": "self_context", "id": "alice", "rev": "0123456789abcdef"}
    tok = se.pending_tap_token("approve", item)
    events = [{"source": "owner_review", "ts": 1.0,
               "attrs": {"action": "promote", "kind": "self_context",
                         "item_id": "alice"}}]
    out = se.annotate_decided_tokens(f"tap {tok} now", user_id="alice",
                                     live_items=[], events=events)
    assert out != f"tap {tok} now" and "approved" in out


# --- the Telegram seat ------------------------------------------------------ #

@pytest.fixture
def tg_env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", DEFAULT_INSTANCE_ID)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    return tmp_path


def _tg(command, text):
    from core.surfaces.dispatcher import RouteDecision, RouteKind
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    from surfaces.telegram.inbound import InboundResult
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(text=text, identity=Identity(
        user_id="alice", source=src, raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:dm:555:alice", command=command))


class _Agent:
    def __init__(self, data_dir):
        class _C:
            config = type("Cfg", (), {"data_dir": data_dir})()

            def get_service(self, name):
                return None
        self.container = _C()


@pytest.mark.asyncio
async def test_telegram_tap_decides_the_shown_rev_and_refuses_a_changed_one(tg_env):
    from surfaces.telegram.harness import act_on_inbound
    w = _draft(tg_env)
    [item] = _items(tg_env)
    tok = se.pending_tap_token("approve", item)
    _patch(w)
    out = await act_on_inbound(_Agent(str(tg_env)), _tg("/approve", tok))
    assert "changed" in out.lower()
    assert w.read_active("alice") == ""
    [fresh] = _items(tg_env)
    out = await act_on_inbound(_Agent(str(tg_env)),
                               _tg("/approve", se.pending_tap_token("approve", fresh)))
    assert "promoted" in out.lower(), out
    assert "long replies" in w.read_active("alice")


@pytest.mark.asyncio
async def test_telegram_bare_id_on_a_never_sent_draft_is_refused(tg_env):
    from surfaces.telegram.harness import act_on_inbound
    w = _draft(tg_env)
    out = await act_on_inbound(_Agent(str(tg_env)), _tg("/approve", "/approve alice"))
    assert "never shown" in out
    assert w.read_active("alice") == ""


# --- the console seat ------------------------------------------------------- #

def _console(monkeypatch, home):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webview.inbox as mod
    monkeypatch.setattr(mod, "_tenant", lambda request: "alice")
    monkeypatch.setattr(mod, "_data_dir", lambda: str(home))
    monkeypatch.setattr(mod, "_instance_id", lambda: DEFAULT_INSTANCE_ID)
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


def test_console_decision_carries_the_card_rev(tmp_path, monkeypatch):
    from surfaces.inbox_sources import collect_self_evolution
    w = _draft(tmp_path)
    [card] = collect_self_evolution("alice", data_dir=str(tmp_path),
                                    instance_id=DEFAULT_INSTANCE_ID)
    rev = card.extra["rev"]
    assert rev == w.list_pending("alice")["rev"]
    client = _console(monkeypatch, tmp_path)
    # No rev and no card sent: refused.
    body = client.post("/api/webgate/inbox/self_context/alice/decide").json()
    assert body["ok"] is False and "never shown" in body["message"]
    _patch(w)
    body = client.post("/api/webgate/inbox/self_context/alice/decide",
                       json={"rev": rev}).json()
    assert body["ok"] is False and "changed" in body["message"]
    fresh = w.list_pending("alice")["rev"]
    body = client.post("/api/webgate/inbox/self_context/alice/decide",
                       json={"rev": fresh}).json()
    assert body["ok"] is True, body
    assert "long replies" in w.read_active("alice")


# -- 098 review: the bulk seats ------------------------------------------------

def test_bulk_reject_binds_to_the_listed_rev_and_discards_an_unshown_draft(tmp_path):
    w = _draft(tmp_path)
    ok_n, fail_n, msgs = se.decide_all(False, user_id="alice", home_dir=tmp_path,
                                       instance_id=DEFAULT_INSTANCE_ID)
    assert (ok_n, fail_n) == (1, 0), msgs
    assert w.list_pending("alice") is None


def test_bulk_approve_holds_an_unshown_draft_with_its_decide_line(tmp_path):
    from tools.controller.approval_queue import decide_all_pending
    w = _draft(tmp_path)
    ok_n, fail_n, msgs = decide_all_pending(approve=True, user_id="alice", home_dir=tmp_path,
                                            instance_id=DEFAULT_INSTANCE_ID)
    assert (ok_n, fail_n) == (0, 0), msgs
    text = "\n".join(msgs)
    assert "never shown" in text
    rev = _items(tmp_path)[0]["rev"]
    assert f"polyrob owner promote self_context alice@{rev}" in text
    assert w.read_active("alice") == ""  # never approved blind


def test_bulk_reject_seat_discards_an_unshown_draft(tmp_path):
    from tools.controller.approval_queue import decide_all_pending
    w = _draft(tmp_path)
    ok_n, fail_n, msgs = decide_all_pending(approve=False, user_id="alice", home_dir=tmp_path,
                                            instance_id=DEFAULT_INSTANCE_ID)
    assert (ok_n, fail_n) == (1, 0), msgs
    assert w.list_pending("alice") is None


def test_the_refusal_names_the_way_out():
    assert "<id>@<rev>" in se.DRAFT_NOT_SHOWN


def test_a_typed_id_at_rev_resolves_on_the_chat_seat(tmp_path):
    """098 review: `/approve alice@<rev>` (the form owner pending prints) names
    the item and carries the rev; an unknown id still resolves to nothing."""
    from tools.controller.approval_queue import resolve_pending_target
    _draft(tmp_path)
    items = _items(tmp_path)
    rev = items[0]["rev"]
    hit = resolve_pending_target(f"alice@{rev}", items)
    assert hit is not None and hit["seat_rev"] == rev
    assert resolve_pending_target(f"bob@{rev}", items) is None
