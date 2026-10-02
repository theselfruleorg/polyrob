from core.surfaces.room_caps import RoomCaps


def test_member_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_MEMBER_COOLDOWN_SEC", "20")
    c = RoomCaps(str(tmp_path / "s.db"))
    ok, _ = c.may_trigger("telegram", "-1", "u1", is_bot=False, now=1000.0)
    assert ok
    c.record_trigger("telegram", "-1", "u1", is_bot=False, now=1000.0)
    ok, why = c.may_trigger("telegram", "-1", "u1", is_bot=False, now=1005.0)
    assert not ok and "cooldown" in why
    ok, _ = c.may_trigger("telegram", "-1", "u2", is_bot=False, now=1005.0)
    assert ok


def test_bot_loop_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_BOT_LOOP_MAX", "3")
    monkeypatch.setenv("GROUP_BOT_LOOP_WINDOW_SEC", "300")
    monkeypatch.setenv("GROUP_BOT_LOOP_COOLDOWN_SEC", "600")
    c = RoomCaps(str(tmp_path / "s.db"))
    for i in range(3):
        c.record_trigger("telegram", "-1", f"bot{i}", is_bot=True, now=100.0 + i)
    ok, why = c.may_trigger("telegram", "-1", "bot9", is_bot=True, now=105.0)
    assert not ok and "bot loop" in why
    ok, _ = c.may_trigger("telegram", "-1", "human", is_bot=False, now=105.0)
    assert ok  # humans never counted


def test_reply_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_REPLY_CAP_PER_HOUR", "2")
    c = RoomCaps(str(tmp_path / "s.db"))
    c.record_reply("telegram", "-1", now=10.0)
    c.record_reply("telegram", "-1", now=11.0)
    ok, why = c.may_reply("telegram", "-1", now=12.0)
    assert not ok and "cap" in why
    ok, _ = c.may_reply("telegram", "-1", now=12.0 + 3601)
    assert ok


def test_prune_drops_old_room_events_only(tmp_path):
    """AC5: room_events grew one row per trigger/reply forever."""
    import sqlite3
    caps = RoomCaps(str(tmp_path / "surfaces.db"))
    now = 100 * 86400.0
    caps.record_reply("telegram", "-1", now=now - 30 * 86400)
    caps.record_reply("telegram", "-1", now=now - 60)
    removed = caps.prune(max_age_secs=7 * 86400, now=now)
    assert removed == 1
    n = sqlite3.connect(str(tmp_path / "surfaces.db")).execute(
        "SELECT COUNT(*) FROM room_events").fetchone()[0]
    assert n == 1


def test_prune_never_cuts_inside_a_read_window(tmp_path, monkeypatch):
    """A horizon shorter than a window a cap reads would reset the cap."""
    caps = RoomCaps(str(tmp_path / "surfaces.db"))
    now = 100 * 86400.0
    caps.record_reply("telegram", "-1", now=now - 3000)
    assert caps.prune(max_age_secs=10, now=now) == 0
    assert caps.replies_since("telegram", "-1", now - 86400) == 1
