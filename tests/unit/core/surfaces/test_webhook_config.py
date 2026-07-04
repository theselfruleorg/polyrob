from agents.task.surface_config import SurfaceConfig


def test_webhook_secret_reads_env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SECRET", "shh")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "vt")
    assert SurfaceConfig.webhook_secret("whatsapp") == "shh"
    assert SurfaceConfig.webhook_verify_token("whatsapp") == "vt"
    assert SurfaceConfig.webhook_secret("unset") is None
