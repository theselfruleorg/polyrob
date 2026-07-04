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
