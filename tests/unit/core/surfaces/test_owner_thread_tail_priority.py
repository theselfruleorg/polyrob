"""F5 (prod review 2026-10-29): a fresh owner session's tail is the CONVERSATION.

Only 19 % of thread rows are owner comments; verb results (avg 880 chars), tx
notices, cards and cron reports filled the 12-row / 2,000-char tail, and the
10-26/10-27 bounty decisions were not in the 10-28 08:00 session's tail. The
tail now renders owner comments and agent replies in full first, collapses every
other row to one short line, and gets a larger block than a delta.
"""
import time

from core.surfaces import owner_thread as ot


def _row(i, direction, body, *, source="", kind="", session_id="s-old"):
    return {"direction": direction, "ts": 1_000 + i, "via": "telegram", "source": source,
            "kind": kind, "session_id": session_id, "body": body}


def _mixed_rows():
    rows = []
    i = 0
    for n in range(6):
        rows.append(_row(i, "in", f"OWNER DECISION {n}: pay in PNL, 48 h window", kind="comment")); i += 1
        rows.append(_row(i, "out", f"AGENT REPLY {n}: noted, I will pay in PNL", source="reply")); i += 1
        rows.append(_row(i, "out", "VERB RESULT " + "x" * 880, source="owner_verb")); i += 1
        rows.append(_row(i, "out", "TX NOTICE " + "y" * 600, source="tx_execution")); i += 1
        rows.append(_row(i, "out", "CARD " + "z" * 500, source="action_card")); i += 1
        rows.append(_row(i, "in", "/send 10 PNL", kind="command")); i += 1
    return rows


def test_the_tail_block_is_larger_than_a_delta():
    assert ot.TAIL_BLOCK_MAX_CHARS >= 3 * ot.BLOCK_MAX_CHARS
    assert ot.tail_rows() >= 40


def test_every_owner_comment_and_reply_survives_the_machine_rows():
    block = ot.render_block(_mixed_rows(), kind="tail", this_session="s-new")
    assert len(block) <= ot.TAIL_BLOCK_MAX_CHARS + 100
    for n in range(6):
        assert f"OWNER DECISION {n}" in block
        assert f"AGENT REPLY {n}" in block


def test_machine_rows_are_one_short_line_each():
    block = ot.render_block(_mixed_rows()[:6], kind="tail")
    verb = next(ln for ln in block.splitlines() if "VERB RESULT" in ln)
    assert len(verb) <= ot.COLLAPSED_LINE_MAX_CHARS + 120
    assert "not sent by you" in verb.lower()
    tx = next(ln for ln in block.splitlines() if "TX NOTICE" in ln)
    assert len(tx) <= ot.COLLAPSED_LINE_MAX_CHARS + 120


def test_lines_stay_in_time_order():
    block = ot.render_block(_mixed_rows(), kind="tail")
    pos = [block.index(f"OWNER DECISION {n}") for n in range(6)]
    assert pos == sorted(pos)


def test_a_harness_fallback_notice_reads_as_an_agent_reply():
    row = _row(0, "out", "I could not finish: the model failed. " + "w" * 300,
               source="turn_fallback")
    assert ot.is_conversation_row(row)
    block = ot.render_block([row], kind="tail")
    assert "w" * 300 in block           # rendered in full, not collapsed


def test_a_delta_keeps_its_own_small_cap():
    big = [_row(i, "out", "x" * 300, source="reply") for i in range(40)]
    out = ot.render_block(big, kind="delta")
    assert len(out) <= ot.BLOCK_MAX_CHARS + 100


def test_thread_tail_reads_forty_rows_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OWNER_THREAD_TAIL_ROWS", raising=False)

    class _C:
        class config:
            data_dir = str(tmp_path)

        def get_service(self, name):
            return None

        def register_service(self, name, svc):
            pass

    c = _C()
    now = time.time()
    for i in range(50):
        ot.record_owner_out(c, "u1", f"line {i}", via="telegram", session_id="s",
                            source="reply", now=now - 50 + i)
    assert len(ot.thread_tail(c, "u1")) == 40
