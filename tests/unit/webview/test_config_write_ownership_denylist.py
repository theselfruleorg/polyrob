"""S8 (2026-09-14): the console PATCH surface cannot hand over the instance.

`PATCH /api/webgate/config/{key}` writes `Path.cwd()/.polyrob/.env`, which
`polyrob.service` loads AFTER `/etc/polyrob/polyrob.env` — so before this guard a
console write could rewrite `ALLOWED_TELEGRAM_USER_IDS` and make an attacker the
owner on the next restart. The refusal must hold at the OWNER (`local`) posture,
which is where the console is most trusted, and the GET payload must say so.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

HANDOVER_KEYS = [
    "ALLOWED_TELEGRAM_USER_IDS",   # who the agent obeys
    "POLYROB_OWNER_TELEGRAM_ID",
    "WALLET_DAILY_CAP_USD",        # money bounds
    "DEFI_AUTONOMOUS_MAX_USD",
    "DEFI_SOLANA_RPC",
    "AGENT_WALLET_MASTER_SEED",    # the key itself
    "PAYMENT_APPROVAL_MODE",       # whether a spend needs a tap
    "APPROVAL_PROVIDER",
    "POLYROB_LOCAL",               # posture / trust
    "AUTONOMY_MODE",
    "AGENT_COMPUTE_POSTURE",
    "WEBVIEW_READ_ONLY",           # the console's own gating
    "WEBGATE_MULTITENANT",
    "CODE_EXEC_ENABLED",
    "SHELL_TOOLS_ENABLED",
]


def _client(monkeypatch, tmp_path, posture="local"):
    import webview.pages as pages
    from webview import webgate
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(webgate, "posture", lambda: posture)
    monkeypatch.setattr(webgate, "read_only", lambda: False)
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))


@pytest.mark.parametrize("key", HANDOVER_KEYS)
def test_handover_flag_patch_is_403_at_owner_posture(monkeypatch, tmp_path, key):
    c = _client(monkeypatch, tmp_path)
    r = c.patch(f"/api/webgate/config/{key}", json={"value": "x"})
    assert r.status_code == 403, key
    assert "not writable from the console" in r.json()["error"], key
    # and nothing reached the env file
    env = tmp_path / ".polyrob" / ".env"
    assert not env.exists() or key not in env.read_text(), key


def test_ordinary_flag_still_writable(monkeypatch, tmp_path):
    """The denylist must not turn the config page into a read-only page."""
    c = _client(monkeypatch, tmp_path)
    r = c.patch("/api/webgate/config/GOALS_ENABLED", json={"value": "on"})
    assert r.status_code == 200
    assert "GOALS_ENABLED=on" in (tmp_path / ".polyrob" / ".env").read_text()


def test_get_marks_handover_flags_unwritable(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    rows = c.get("/api/webgate/config",
                 params={"query": "ALLOWED_TELEGRAM_USER_IDS"}).json()["settings"]
    row = next(r for r in rows if r["key"] == "ALLOWED_TELEGRAM_USER_IDS")
    assert row["console_writable"] is False
