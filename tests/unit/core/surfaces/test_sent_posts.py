"""0008: the post ledger — what the agent sent, with the surface's own message ids."""
from core.surfaces.sent_posts import SentPosts


def _store(tmp_path):
    return SentPosts(str(tmp_path / "surfaces.db"))


def test_record_get_roundtrip(tmp_path):
    s = _store(tmp_path)
    row = s.record("telegram", "-1001", ["11", "12"], "hello den", now=1000.0)
    assert isinstance(row, int) and row > 0
    p = s.get(row)
    assert p.surface == "telegram" and p.chat_id == "-1001"
    assert p.message_ids == ["11", "12"]
    assert p.preview == "hello den"
    assert p.ts == 1000.0 and p.deleted_ts is None


def test_record_without_ids_is_not_written(tmp_path):
    s = _store(tmp_path)
    assert s.record("telegram", "-1001", [], "x") is None
    assert s.recent("telegram", "-1001") == []


def test_preview_is_scrubbed_and_bounded(tmp_path):
    s = _store(tmp_path)
    key = "sk-" + "a" * 48
    row = s.record("telegram", "1", ["5"], f"key {key} " + "y" * 1000)
    p = s.get(row)
    assert key not in p.preview
    assert len(p.preview) <= 201


def test_recent_newest_first_skips_deleted(tmp_path):
    s = _store(tmp_path)
    a = s.record("telegram", "-1001", ["1"], "a", now=1.0)
    b = s.record("telegram", "-1001", ["2"], "b", now=2.0)
    s.record("telegram", "-2002", ["3"], "other chat", now=3.0)
    assert [p.row for p in s.recent("telegram", "-1001", 5)] == [b, a]
    s.mark_deleted(b, now=4.0)
    assert [p.row for p in s.recent("telegram", "-1001", 5)] == [a]
    assert s.get(b).deleted_ts == 4.0
    # no chat filter: every chat, newest first
    assert len(s.recent("telegram", None, 10)) == 2


def test_find_by_message_id_is_chat_scoped(tmp_path):
    s = _store(tmp_path)
    row = s.record("telegram", "-1001", ["41", "42"], "two chunks")
    assert s.find("telegram", "-1001", "42").row == row
    assert s.find("telegram", "-2002", "42") is None
    assert s.find("telegram", "-1001", "4") is None


def test_old_rows_are_pruned_on_record(tmp_path):
    s = _store(tmp_path)
    old = s.record("telegram", "1", ["1"], "old", now=0.0)
    s.record("telegram", "1", ["2"], "new", now=30 * 86400.0)
    assert s.get(old) is None
