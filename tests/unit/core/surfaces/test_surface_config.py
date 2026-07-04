import importlib


def test_flags_default_off(monkeypatch):
    for k in ("SINGULAR_CHAT_ENABLED", "TELEGRAM_SURFACE_ENABLED", "CHAT_INTENT_CLASSIFIER"):
        monkeypatch.delenv(k, raising=False)
    sc = importlib.import_module("agents.task.surface_config")
    assert sc.SurfaceConfig.singular_chat_enabled() is False
    assert sc.SurfaceConfig.telegram_surface_enabled() is False
    assert sc.SurfaceConfig.chat_intent_classifier_enabled() is False


def test_flags_flip_on(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_SURFACE_ENABLED", "1")
    sc = importlib.import_module("agents.task.surface_config")
    assert sc.SurfaceConfig.singular_chat_enabled() is True
    assert sc.SurfaceConfig.telegram_surface_enabled() is True


def test_falsey_values_disable(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "off")
    sc = importlib.import_module("agents.task.surface_config")
    assert sc.SurfaceConfig.singular_chat_enabled() is False
