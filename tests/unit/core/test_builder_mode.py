"""AGENT_BUILDER_MODE=off|build|ship — publishing defaults only (032)."""
import pytest

from core.config_policy import builder_mode as bm


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_BUILDER_MODE", "APP_SERVICE_BASE_DOMAIN", "APP_SERVICE_CERT_DIR",
              "PUBLISH_ENABLED", "GITHUB_TOOL_ENABLED", "APP_SERVICE_ENABLED",
              "APP_SERVICE_ALLOW_PUBLIC"):
        monkeypatch.delenv(k, raising=False)
    bm.reset_builder_mode_warnings()
    yield
    bm.reset_builder_mode_warnings()


def _flags():
    from core.app_service.config import app_service_allow_public, app_service_enabled
    from tools.github import github_enabled
    from tools.publish import publish_enabled
    return (publish_enabled(), github_enabled(), app_service_enabled(), app_service_allow_public())


def test_off_is_byte_identical():
    assert bm.agent_builder_mode() == "off" and bm.effective_builder_mode() == "off"
    assert _flags() == (False, False, False, False)


def test_unknown_degrades_to_off(monkeypatch):
    monkeypatch.setenv("AGENT_BUILDER_MODE", "yolo")
    assert bm.agent_builder_mode() == "off"


def test_build_moves_publish_and_github_only(monkeypatch):
    monkeypatch.setenv("AGENT_BUILDER_MODE", "build")
    assert _flags() == (True, True, False, False)
    monkeypatch.setenv("PUBLISH_ENABLED", "false")  # explicit env wins
    assert _flags()[0] is False


def test_ship_clamps_without_domain_or_cert(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("AGENT_BUILDER_MODE", "ship")
    assert bm.ship_clamp_reason() == "APP_SERVICE_BASE_DOMAIN is not set"
    assert bm.effective_builder_mode() == "build"
    assert _flags() == (True, True, False, False)
    assert "clamped" in bm.builder_mode_display()
    monkeypatch.setenv("APP_SERVICE_BASE_DOMAIN", "apps.example.test")
    monkeypatch.setenv("APP_SERVICE_CERT_DIR", str(tmp_path / "cert"))
    assert "no certificate at" in bm.ship_clamp_reason()
    assert bm.effective_builder_mode() == "build"


def test_ship_effective_with_domain_and_cert(monkeypatch, tmp_path):
    (tmp_path / "cert").mkdir()
    (tmp_path / "cert" / "fullchain.pem").write_text("x")
    monkeypatch.setenv("AGENT_BUILDER_MODE", "ship")
    monkeypatch.setenv("APP_SERVICE_BASE_DOMAIN", "apps.example.test")
    monkeypatch.setenv("APP_SERVICE_CERT_DIR", str(tmp_path / "cert"))
    assert bm.ship_clamp_reason() is None and bm.effective_builder_mode() == "ship"
    assert _flags() == (True, True, True, True)
    assert bm.builder_mode_display() == "ship"
    monkeypatch.setenv("APP_SERVICE_ALLOW_PUBLIC", "false")
    assert _flags()[3] is False


def test_ship_never_moves_money_or_host(monkeypatch, tmp_path):
    (tmp_path / "cert").mkdir()
    (tmp_path / "cert" / "fullchain.pem").write_text("x")
    monkeypatch.setenv("AGENT_BUILDER_MODE", "ship")
    monkeypatch.setenv("APP_SERVICE_BASE_DOMAIN", "apps.example.test")
    monkeypatch.setenv("APP_SERVICE_CERT_DIR", str(tmp_path / "cert"))
    for flag in ("X402_PAY_ENABLED", "DEFI_TRADE_ENABLED", "AGENT_COMPUTE_POSTURE",
                 "SELF_ENV_ENABLED", "HF_DEPLOY_ENABLED"):
        assert bm._builder_capability_default(flag) is False, flag
