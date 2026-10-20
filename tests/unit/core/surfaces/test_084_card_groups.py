"""084: the agent comes back when the owner has decided every card it raised.

Prod 2026-10-19: seven payout cards, 14 owner taps, and the agent never
continued until the owner typed. Covered here: groups, lineage, settlement
(exactly one wake row), the sweep, the outcomes text and the turn-start read.
"""
import time

import pytest

from core.surfaces import card_groups as G
from core.surfaces import cards
from core.wake_queue import get_wake_queue

U, S = "owner1", "sess-chat"
LINE = ["24", "0x" + "ab" * 20, "to", "0x" + "cd" * 20, "on", "robinhood"]


@pytest.fixture
def st(tmp_path):
    store = cards.CardStore(str(tmp_path / "cards.db"))
    cards.set_store(store)
    G._GROUPS.clear()
    yield store
    cards.set_store(None)
    G._GROUPS.clear()


def _wakes(st):
    return get_wake_queue(G.wake_queue_path(st))


def _propose(st, then=""):
    gid = G.groups(st).open_group_for(U, S)
    if then:
        G.groups(st).set_then(gid, then)
    return cards.proposal_card(U, "/send", LINE, why="raffle", session_id=S, group_id=gid)


def _quote_of(p):
    """What the seat does on "Get a quote": replace, then make the quote card
    while running the line under the proposal's lineage."""
    pr = cards.press(p.card_id, "re", U)
    assert pr.run
    with cards.lineage(pr.card):
        return cards._new(U, cards.KIND_QUOTE, cards.ORIGIN_SYSTEM, "/send — quote",
                          verb="/send", args=LINE + ["max", "24.5"], typed=LINE,
                          ttl=cards.QUOTE_TTL_S)


def _confirm(q, result):
    assert cards.press(q.card_id, "ok", U).run
    return cards.finish(q.card_id, result)


def test_one_session_proposals_share_one_open_group(st):
    a, b = _propose(st), _propose(st)
    assert a.group_id and a.group_id == b.group_id
    assert G.groups(st).get(a.group_id)["state"] == G.G_OPEN


def test_a_quote_inherits_session_group_and_parent(st):
    p = _propose(st)
    q = _quote_of(p)
    assert (q.session_id, q.group_id, q.parent_id) == (S, p.group_id, p.card_id)


def test_the_group_settles_once_when_every_card_is_decided(st):
    p1, p2 = _propose(st, then="pay the PNL half next"), _propose(st)
    q1 = _quote_of(p1)
    assert G.groups(st).get(p1.group_id)["state"] == G.G_OPEN   # replaced ≠ decided
    _confirm(q1, "SENT AND CONFIRMED 24 USDG tx 0xc5")
    assert G.groups(st).get(p1.group_id)["state"] == G.G_OPEN
    q2 = _quote_of(p2)
    _confirm(q2, "❌ Nothing was sent. VALUE NOT VERIFIED")
    g = G.groups(st).get(p1.group_id)
    assert g["state"] == G.G_SETTLED
    assert _wakes(st).pending_count() == 1
    text = G.render_outcomes(U, p1.group_id, st=st)
    assert "→ DONE: SENT AND CONFIRMED" in text and "→ FAILED: ❌ Nothing was sent" in text
    assert "pay the PNL half next" in text
    assert text.count("/send 24") == 2           # the replaced proposals are not repeated


def test_a_new_proposal_after_settling_starts_a_new_group(st):
    p = _propose(st)
    cards.press(p.card_id, "no", U)
    assert G.groups(st).get(p.group_id)["state"] == G.G_SETTLED
    assert _propose(st).group_id != p.group_id


def test_a_refused_quote_settles_through_its_noted_result(st):
    p = _propose(st)
    cards.press(p.card_id, "re", U)
    assert G.groups(st).get(p.group_id)["state"] == G.G_OPEN
    cards.note_result(p.card_id, "🔒 refused: per-transaction cap")
    assert G.groups(st).get(p.group_id)["state"] == G.G_SETTLED
    assert "NO QUOTE: 🔒 refused" in G.render_outcomes(U, p.group_id, st=st)


def test_an_open_recovery_card_keeps_the_group_open(st):
    p = _propose(st)
    q = _quote_of(p)
    cards.press(q.card_id, "ok", U)
    with cards.lineage(cards.store().get(q.card_id)):
        ack = cards._new(U, cards.KIND_PROPOSAL, cards.ORIGIN_SYSTEM, "extra check",
                         verb="/send", args=LINE + ["anyway"], ttl=cards.QUOTE_TTL_S)
    cards.finish(q.card_id, "❌ Nothing was sent. VALUE NOT VERIFIED")
    assert ack.group_id == p.group_id
    assert G.groups(st).get(p.group_id)["state"] == G.G_OPEN
    cards.press(ack.card_id, "no", U)
    assert G.groups(st).get(p.group_id)["state"] == G.G_SETTLED


def test_the_sweep_expires_a_walked_away_group(st):
    p = _propose(st)
    conn = st._conn()
    conn.execute("UPDATE cards SET expires_at=? WHERE card_id=?", (time.time() - 1, p.card_id))
    conn.commit()
    conn.close()
    assert cards.sweep() == 1
    assert G.groups(st).get(p.group_id)["state"] == G.G_SETTLED
    assert "→ EXPIRED" in G.render_outcomes(U, p.group_id, st=st)


def test_a_late_result_after_the_stuck_sweep_writes_no_second_wake(st):
    p = _propose(st)
    q = _quote_of(p)
    cards.press(q.card_id, "ok", U)
    conn = st._conn()
    conn.execute("UPDATE cards SET updated_at=? WHERE card_id=?",
                 (time.time() - cards.STUCK_AFTER_S - 5, q.card_id))
    conn.commit()
    conn.close()
    cards.sweep()
    assert _wakes(st).pending_count() == 1
    cards.finish(q.card_id, "SENT AND CONFIRMED")
    assert cards.store().get(q.card_id).state == cards.S_DONE
    assert _wakes(st).pending_count() == 1


def test_refresh_on_an_expired_quote_runs_a_fresh_quote(st):
    p = _propose(st)
    q = _quote_of(p)
    conn = st._conn()
    conn.execute("UPDATE cards SET expires_at=? WHERE card_id=?", (time.time() - 1, q.card_id))
    conn.commit()
    conn.close()
    pr = cards.press(q.card_id, "re", U)
    assert pr.run == q.refresh_line
    assert pr.card.state == cards.S_REPLACED
    assert G.groups(st).get(p.group_id)["state"] == G.G_OPEN


def test_the_turn_start_block_shows_a_settled_group_once(st):
    p = _propose(st)
    cards.press(p.card_id, "no", U)
    text = G.take_outcomes(session_id=S, user_id=U, st=st)
    assert text.startswith("<card-outcomes>") and "DISMISSED" in text
    assert G.groups(st).get(p.group_id)["state"] == G.G_CONSUMED
    assert G.take_outcomes(session_id=S, user_id=U, st=st) == ""


def test_the_turn_start_block_skips_a_woken_group_and_names_open_ones(st):
    p = _propose(st)
    cards.press(p.card_id, "no", U)
    G.groups(st).transition(p.group_id, frozenset({G.G_SETTLED}), G.G_WOKEN)
    _propose(st)
    text = G.take_outcomes(session_id=S, user_id=U, st=st)
    assert "DISMISSED" not in text and "Still waiting on the owner: 1" in text


def test_a_read_without_a_cards_db_creates_none(tmp_path, monkeypatch):
    cards.set_store(None)
    monkeypatch.setattr("core.runtime_paths.data_home_db_path",
                        lambda name, **kw: str(tmp_path / name))
    assert G.take_outcomes(session_id=S, user_id=U, st=cards.existing_store()) == ""
    assert not (tmp_path / "cards.db").exists()


def test_release_puts_a_wake_back_without_spending_an_attempt(st):
    q = _wakes(st)
    q.enqueue(S, U, "x", kind=G.WAKE_KIND, metadata={"kind": G.WAKE_KIND})
    row = q.claim_pending("me")[0]
    q.release(row.id)
    again = q.claim_pending("me")[0]
    assert again.id == row.id and again.attempts == 1


def test_a_lost_wake_row_reopens_the_group_and_the_sweep_heals_it(st, monkeypatch):
    import core.wake_queue as wq
    real = wq.get_wake_queue

    class _Broken:
        def enqueue(self, *a, **kw):
            raise RuntimeError("disk full")

    monkeypatch.setattr(wq, "get_wake_queue", lambda path: _Broken())
    p = _propose(st)
    cards.press(p.card_id, "no", U)
    assert G.groups(st).get(p.group_id)["state"] == G.G_OPEN
    monkeypatch.setattr(wq, "get_wake_queue", real)
    assert G.heal(st) == 1
    assert G.groups(st).get(p.group_id)["state"] == G.G_SETTLED
    assert _wakes(st).pending_count() == 1
    assert G.heal(st) == 0

