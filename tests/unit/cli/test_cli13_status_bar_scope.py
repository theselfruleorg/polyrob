"""CLI13 (2026-10-03 audit): the status-bar autonomy line reads the deployed home
and owner — the same scope as `/autonomy` — not the shell's ``config.data_dir``."""
from cli.ui import autonomy_poll


def test_status_bar_reads_the_deployed_scope(monkeypatch):
    seen = {}
    monkeypatch.setattr("cli._admin_home.admin_data_dir", lambda write=None: "/deployed")
    monkeypatch.setattr("cli._admin_home.admin_owner_tenant", lambda uid=None: "tg_owner")
    monkeypatch.setattr(autonomy_poll, "read_autonomy_snapshot",
                        lambda uid, d: seen.update(uid=uid, d=d) or {"ok": 1})
    assert autonomy_poll.poll_status_bar("local") == {"ok": 1}
    assert seen == {"uid": "tg_owner", "d": "/deployed"}


def test_a_refusing_seam_degrades_to_local(monkeypatch):
    def boom(write=None):
        raise RuntimeError("unreadable deployed home")
    seen = {}
    monkeypatch.setattr("cli._admin_home.admin_data_dir", boom)
    monkeypatch.setattr(autonomy_poll, "read_autonomy_snapshot",
                        lambda uid, d: seen.update(uid=uid, d=d))
    autonomy_poll.poll_status_bar("local")
    assert seen["uid"] == "local" and seen["d"]


def test_chat_uses_the_helper():
    import inspect
    from cli.commands import chat
    src = inspect.getsource(chat)
    assert "poll_status_bar(user_id)" in src
    assert "read_autonomy_snapshot(user_id" not in src
