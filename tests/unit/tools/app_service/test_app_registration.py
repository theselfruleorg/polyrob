"""Gated registration + the builder-mode defaults (032)."""


def test_register_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("APP_SERVICE_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_BUILDER_MODE", raising=False)
    from tools.app_service import register_app_service_tool
    assert register_app_service_tool() is False


def test_flag_default_is_off_and_local_does_not_flip_it(monkeypatch):
    monkeypatch.delenv("APP_SERVICE_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_BUILDER_MODE", raising=False)
    from core.app_service.config import app_service_allow_public, app_service_enabled
    assert app_service_enabled() is False and app_service_allow_public() is False
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert app_service_enabled() is False


def test_register_when_enabled(monkeypatch):
    monkeypatch.setenv("APP_SERVICE_ENABLED", "true")
    from tools.app_service import register_app_service_tool
    from tools.descriptors import TOOL_DESCRIPTORS
    assert register_app_service_tool() is True
    assert "app_service" in TOOL_DESCRIPTORS
    assert TOOL_DESCRIPTORS["app_service"].tool_class is not None


def test_never_in_default_tool_ids(monkeypatch):
    monkeypatch.setenv("APP_SERVICE_ENABLED", "true")
    from agents.task.tool_defaults import resolve_toolset
    for name in ("default", "coding", "autonomy", "full"):
        try:
            tools = resolve_toolset(name)
        except Exception:
            continue
        assert "app_service" not in (tools or []), name


def test_config_defaults(monkeypatch):
    for k in ("APP_SERVICE_MAX_LIVE", "APP_SERVICE_DAILY_MAX", "APP_SERVICE_MIN_INTERVAL_SEC",
              "APP_SERVICE_PORT_RANGE", "APP_SERVICE_EGRESS_DEFAULT", "APP_SERVICE_IMAGE",
              "CODE_EXEC_DEV_IMAGE", "APP_SERVICE_BASE_DOMAIN", "APP_SERVICE_CERT_DIR"):
        monkeypatch.delenv(k, raising=False)
    from core.app_service import config as c
    assert (c.app_service_max_live(), c.app_service_daily_max(), c.app_service_min_interval_sec()) == (3, 10, 120)
    assert c.app_service_port_range() == (18000, 18099)
    monkeypatch.setenv("APP_SERVICE_PORT_RANGE", "garbage")
    assert c.app_service_port_range() == (18000, 18099)
    monkeypatch.setenv("APP_SERVICE_PORT_RANGE", "20000-20010")
    assert c.app_service_port_range() == (20000, 20010)
    assert c.app_service_egress_default() == "none"
    monkeypatch.setenv("APP_SERVICE_EGRESS_DEFAULT", "open")
    assert c.app_service_egress_default() == "open"
    assert c.app_service_image() == "polyrob-dev:latest"
    monkeypatch.setenv("CODE_EXEC_DEV_IMAGE", "polyrob-dev:node-chromium")
    assert c.app_service_image() == "polyrob-dev:node-chromium"
    monkeypatch.setenv("APP_SERVICE_BASE_DOMAIN", "Apps.Example.Test.")
    assert c.app_service_base_domain() == "apps.example.test"
    assert c.app_service_cert_dir() == "/etc/letsencrypt/live/apps.example.test"
    assert c.safe_tenant("Owner 1!") == "owner-1" and c.safe_tenant("") == "tenant"
    assert c.logs_path("/d", "u1", "st") == "/d/apps/u1/st/logs.txt"
