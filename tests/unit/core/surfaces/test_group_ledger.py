import time

from core.surfaces.group_ledger import GroupLedger, LedgerRow


def _row(mid, text, ts, sender="u1", name="alice", bot=False, role="member"):
    return LedgerRow(surface="telegram", chat_id="-1", thread_id=None, message_id=str(mid), ts=ts,
                     sender_id=sender, sender_name=name, sender_is_bot=bot, role_at_write=role,
                     kind="text", text=text, reply_to_message_id=None, mentions_bot=False)


# These fixtures use tiny synthetic `ts` values (10.0, 11.0, ...) that are not
# meant to exercise retention at all -- only test_retention_is_wall_clock and
# test_prune_by_rows_and_age below test pruning itself. Retention is wall-clock
# (round-1 review), so any small fixed `ts` reads as "ancient" against real
# time.time() and would be pruned out from under these tests unless neutralized.
def _no_retention(monkeypatch):
    monkeypatch.setenv("GROUP_LEDGER_RETENTION_DAYS", "100000")


def test_append_is_idempotent_and_tail_is_oldest_first(tmp_path, monkeypatch):
    _no_retention(monkeypatch)
    lg = GroupLedger(str(tmp_path / "s.db"))
    lg.append(_row(1, "one", 10.0)); lg.append(_row(2, "two", 11.0)); lg.append(_row(1, "one", 10.0))
    rows = lg.tail("telegram", "-1")
    assert [r.text for r in rows] == ["one", "two"]
    assert lg.count("telegram", "-1") == 2


def test_edit_replaces_text(tmp_path, monkeypatch):
    _no_retention(monkeypatch)
    lg = GroupLedger(str(tmp_path / "s.db"))
    lg.append(_row(1, "one", 10.0))
    r = _row(1, "one edited", 12.0); r.kind = "edit"
    lg.append(r)
    assert lg.tail("telegram", "-1")[0].text == "one edited"


def test_secret_shapes_are_scrubbed(tmp_path, monkeypatch):
    _no_retention(monkeypatch)
    lg = GroupLedger(str(tmp_path / "s.db"))
    lg.append(_row(1, "key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnop", 10.0))
    assert "sk-ant-api03" not in lg.tail("telegram", "-1")[0].text


def test_checkpoint_and_unanswered(tmp_path, monkeypatch):
    _no_retention(monkeypatch)
    lg = GroupLedger(str(tmp_path / "s.db"))
    lg.append(_row(1, "q1", 10.0)); lg.append(_row(2, "q2", 20.0))
    assert lg.checkpoint("telegram", "-1", "goal") == 0.0
    lg.advance("telegram", "-1", "goal", 15.0)
    assert [r.text for r in lg.tail("telegram", "-1", since_ts=lg.checkpoint("telegram", "-1", "goal"))] == ["q2"]
    lg.mark_answered("telegram", "-1", ["2"], "sess")
    assert lg.tail("telegram", "-1", unanswered_only=True)[0].text == "q1"


def test_prune_by_rows_and_age(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_LEDGER_MAX_ROWS_PER_CHAT", "2")
    monkeypatch.setenv("GROUP_LEDGER_RETENTION_DAYS", "100000")
    lg = GroupLedger(str(tmp_path / "s.db"))
    for i in range(4):
        lg.append(_row(i, f"m{i}", 10.0 + i))
    assert [r.text for r in lg.tail("telegram", "-1")] == ["m2", "m3"]


def test_retention_is_wall_clock(tmp_path):
    """Round-1 review Critical: retention must be wall-clock, not anchored to
    the chat's own newest row -- otherwise a quiet chat's rows never age out.
    A row 30 days old under the default 14-day retention is expired both by a
    standalone prune() call and, without any explicit prune(), by tail()
    itself (prune-on-read)."""
    lg = GroupLedger(str(tmp_path / "s.db"))
    old_ts = time.time() - 30 * 86400
    lg.append(_row(1, "ancient", old_ts))
    lg.prune("telegram", "-1")
    assert lg.count("telegram", "-1") == 0
    assert lg.tail("telegram", "-1") == []
