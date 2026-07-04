from core.surfaces.session_chat_registry import SessionChatRegistry


def test_bind_then_resolve(tmp_path):
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    reg.bind("k1", session_id="sess_1", user_id="u_abc", surface_id="telegram", chat_id="555")
    row = reg.resolve("k1")
    assert row["session_id"] == "sess_1"
    assert row["user_id"] == "u_abc"
    assert row["surface_id"] == "telegram"


def test_resolve_missing_returns_none(tmp_path):
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    assert reg.resolve("nope") is None


def test_bind_is_idempotent_upsert(tmp_path):
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    reg.bind("k1", "sess_1", "u_abc", "telegram", "555")
    reg.bind("k1", "sess_2", "u_abc", "telegram", "555")  # rebind same key
    assert reg.resolve("k1")["session_id"] == "sess_2"


def test_set_owner(tmp_path):
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    reg.bind("k1", "sess_1", "u_abc", "telegram", "555")
    reg.set_owner("k1", 4242)
    assert reg.resolve("k1")["owner_pid"] == 4242


def test_resolve_by_session_id(tmp_path):
    """Reverse lookup (session_id -> chat row), used to re-attach the outbound surface
    to a recreated orchestrator so a resumed chat isn't mute (#0 mute-on-resume)."""
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    reg.bind("agent:main:telegram:dm:555:u_abc", "sess_1", "u_abc", "telegram", "555")
    row = reg.resolve_by_session_id("sess_1")
    assert row is not None
    assert row["session_key"] == "agent:main:telegram:dm:555:u_abc"
    assert row["surface_id"] == "telegram" and row["chat_id"] == "555"
    assert reg.resolve_by_session_id("nope") is None


def test_purge_stale_deletes_old_rows_and_keeps_fresh(tmp_path):
    """a5: GC drops chat<->session bindings whose last activity is older than the
    horizon, returning the count deleted; fresh rows survive."""
    import time
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    reg.bind("old", "sess_old", "u_abc", "telegram", "1")
    reg.bind("fresh", "sess_fresh", "u_abc", "telegram", "2")
    # Backdate "old" well past the horizon (updated_at is unix seconds).
    from core.sqlite_util import execute_retry
    execute_retry(reg.db_path,
                  "UPDATE session_chat_map SET updated_at = ? WHERE session_key = ?",
                  (time.time() - 10_000, "old"))
    deleted = reg.purge_stale(older_than_secs=3600)
    assert deleted == 1
    assert reg.resolve("old") is None
    assert reg.resolve("fresh") is not None


def test_purge_stale_none_to_purge_returns_zero(tmp_path):
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    reg.bind("fresh", "sess_fresh", "u_abc", "telegram", "2")
    assert reg.purge_stale(older_than_secs=3600) == 0
    assert reg.resolve("fresh") is not None
