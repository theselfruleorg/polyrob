"""default_goal_tools under AGENT_BUILDER_MODE (032): publish under build, app_service
under effective ship, never money, unchanged under off; explicit env still wins."""
import pytest

from core.config_policy import builder_mode as bm


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_BUILDER_MODE", "APP_SERVICE_BASE_DOMAIN", "APP_SERVICE_CERT_DIR",
              "PUBLISH_ENABLED", "APP_SERVICE_ENABLED", "AUTONOMY_MODE"):
        monkeypatch.delenv(k, raising=False)
    bm.reset_builder_mode_warnings()
    yield
    bm.reset_builder_mode_warnings()


def _tools():
    from agents.task.goals.dispatcher import default_goal_tools
    return default_goal_tools()


def test_off_is_unchanged():
    t = _tools()
    assert "publish" not in t and "app_service" not in t


def test_build_adds_publish_only(monkeypatch):
    monkeypatch.setenv("AGENT_BUILDER_MODE", "build")
    t = _tools()
    assert "publish" in t and "app_service" not in t
    monkeypatch.setenv("PUBLISH_ENABLED", "false")
    assert "publish" not in _tools()


def test_ship_clamped_behaves_like_build(monkeypatch):
    monkeypatch.setenv("AGENT_BUILDER_MODE", "ship")
    t = _tools()
    assert "publish" in t and "app_service" not in t


def test_ship_effective_adds_app_service_and_never_money(monkeypatch, tmp_path):
    (tmp_path / "cert").mkdir()
    (tmp_path / "cert" / "fullchain.pem").write_text("x")
    monkeypatch.setenv("AGENT_BUILDER_MODE", "ship")
    monkeypatch.setenv("APP_SERVICE_BASE_DOMAIN", "apps.example.test")
    monkeypatch.setenv("APP_SERVICE_CERT_DIR", str(tmp_path / "cert"))
    t = _tools()
    assert "publish" in t and "app_service" in t
    from core.tool_capabilities import ids_with
    assert not set(t) & ids_with("money")
    assert "self_env" not in t and "hf_deploy" not in t
