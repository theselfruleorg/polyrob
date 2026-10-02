"""Interface audit 2026-10-03 — action cards.

CA1: Refresh on a `/send`/`/swap` quote card re-ran the confirm line WITH the
seat-added `max <old bound>` as if the owner had typed it, so after a >5% move
every refreshed quote kept the stale bound and Confirm failed forever.

AC2: `sweep_stuck` failed a confirmed card after 30 min; a `/bridge` that
waited on `/approve` and then completed could not record its result.

AC5: `cards` and `card_refs` were never pruned.
"""
import time

from core.surfaces import cards

ADDR = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"
TYPED = ["1", "native", "to", ADDR, "on", "ethereum"]
SEND_QUOTE = ("simulated value: $2680.0000\n"
              f"To send it: /send 1 native to {ADDR} on ethereum max 2814.01 go")


def _store(tmp_path):
    return cards.CardStore(str(tmp_path / "c.db"))


def test_refresh_reruns_the_typed_words_not_the_seat_bound(tmp_path):
    st = _store(tmp_path)
    _, card = cards.quote_card("u1", "/send", TYPED, SEND_QUOTE, st=st)
    assert card.confirm_line.endswith("max 2814.01 go")
    p = cards.press(card.card_id, "re", "u1", st=st)
    assert p.run == "/send " + " ".join(TYPED)
    assert "max" not in p.run.split()


def test_a_bound_the_owner_typed_survives_refresh(tmp_path):
    st = _store(tmp_path)
    typed = TYPED + ["max", "3000"]
    quote = f"To send it: /send 1 native to {ADDR} on ethereum max 3000 go"
    _, card = cards.quote_card("u1", "/send", typed, quote, st=st)
    p = cards.press(card.card_id, "re", "u1", st=st)
    assert p.run.endswith("max 3000")


def test_typed_args_persist_across_a_reload(tmp_path):
    st = _store(tmp_path)
    _, card = cards.quote_card("u1", "/send", TYPED, SEND_QUOTE, st=st)
    again = cards.CardStore(st.db_path).get(card.card_id)
    assert again.typed == TYPED


def test_a_late_result_corrects_a_swept_card(tmp_path):
    st = _store(tmp_path)
    _, card = cards.quote_card("u1", "/send", TYPED, SEND_QUOTE, st=st)
    p = cards.press(card.card_id, "ok", "u1", st=st)
    assert p.run
    swept = st.sweep_stuck("u1", older_than_s=-1)
    assert swept and swept[0].state == cards.S_FAILED
    done = cards.finish(card.card_id, "SENT AND CONFIRMED tx 0xabc", st=st)
    assert done is not None and done.state == cards.S_DONE
    assert "SENT AND CONFIRMED" in done.result


def test_a_real_failure_is_never_overwritten(tmp_path):
    st = _store(tmp_path)
    _, card = cards.quote_card("u1", "/send", TYPED, SEND_QUOTE, st=st)
    cards.press(card.card_id, "ok", "u1", st=st)
    cards.finish(card.card_id, "❌ NOT SENT: refused", st=st)
    assert cards.finish(card.card_id, "SENT AND CONFIRMED", st=st) is None
    assert st.get(card.card_id).state == cards.S_FAILED


def test_prune_drops_old_decided_cards_and_their_refs(tmp_path):
    st = _store(tmp_path)
    _, old = cards.quote_card("u1", "/send", TYPED, SEND_QUOTE, st=st)
    _, live = cards.quote_card("u1", "/send", TYPED, SEND_QUOTE, st=st)
    st.add_ref(old.card_id, "telegram", 1, 10)
    st.add_ref(live.card_id, "telegram", 1, 11)
    cards.press(old.card_id, "no", "u1", st=st)
    # Age the decided card past the retention.
    conn = st._conn()
    conn.execute("UPDATE cards SET updated_at=? WHERE card_id=?",
                 (time.time() - cards.DECIDED_RETENTION_S - 10, old.card_id))
    conn.commit()
    conn.close()
    removed = st.prune()
    assert removed == 1
    assert st.get(old.card_id) is None
    assert st.refs(old.card_id) == []
    assert st.get(live.card_id) is not None
    assert st.refs(live.card_id)
