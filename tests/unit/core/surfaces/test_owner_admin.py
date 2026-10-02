"""WS-D: owner/access quick-access summary (single read of the SSOT)."""
from core.surfaces.owner_admin import owner_access_summary


def test_summary_reflects_env(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "false")
    monkeypatch.setenv("EMAIL_SURFACE_ENABLED", "true")
    monkeypatch.delenv("TELEGRAM_SURFACE_ENABLED", raising=False)
    s = owner_access_summary()
    assert s["owner_principal"] == "u_owner"
    assert s["correspondent_access_enabled"] is True
    assert s["require_approval"] is False
    assert s["surfaces"]["email"] is True
    assert s["surfaces"]["telegram"] is False
    assert s["owner_by_email"] is False  # v1: always OFF


def test_summary_no_owner_bound(monkeypatch):
    for k in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID", "SURFACE_SUPER_ADMIN_USER_IDS"):
        monkeypatch.delenv(k, raising=False)
    assert owner_access_summary()["owner_principal"] is None


class _BrokenRegistry:
    def list(self, user_id=None):
        raise RuntimeError("database disk image is malformed")


def test_pending_contacts_unreadable_store_raises_not_empty():
    """AC1: an unreadable correspondent store is not "no pending contacts" —
    the builder must raise so every seat can name the store unreadable."""
    import pytest

    from core.surfaces.owner_admin import pending_correspondent_items
    with pytest.raises(RuntimeError):
        pending_correspondent_items(_BrokenRegistry(), "u1")


def test_all_pending_names_pending_contacts_unavailable(tmp_path):
    from tools.controller.approval_queue import all_pending

    class _Board:
        pass

    res = all_pending(user_id="u1", home_dir=str(tmp_path), instance_id="i",
                      board=_Board(), correspondent_registry=_BrokenRegistry())
    assert "pending contacts" in res.unavailable


def test_status_snapshot_names_unreadable_pending_contacts(tmp_path, monkeypatch):
    """AC1: the approvals section must not read "no pending approvals" over
    a correspondents.db it could not read."""
    import core.status_snapshot as ss
    import sqlite3
    (tmp_path / "correspondents.db").write_bytes(b"x")
    con = sqlite3.connect(str(tmp_path / "goals.db"))
    con.execute("CREATE TABLE goals (id TEXT, title TEXT, payload TEXT, "
                "kind TEXT, status TEXT, user_id TEXT)")
    con.commit()
    con.close()
    monkeypatch.setattr("core.surfaces.correspondents.CorrespondentRegistry",
                        lambda path: _BrokenRegistry())
    monkeypatch.setattr("core.self_evolution.list_pending",
                        lambda *a, **k: [])
    sec = ss._approvals_section("u1", str(tmp_path),
                                str(tmp_path / "goals.db"))
    text = " ".join(sec.lines) + " ".join(h.text for h in sec.health)
    assert "unreadable" in text
    assert "no pending approvals" not in " ".join(sec.lines)
