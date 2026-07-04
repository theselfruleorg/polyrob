def test_gate_default_off(monkeypatch):
    monkeypatch.delenv("OPENAI_COMPAT_API_ENABLED", raising=False)
    from api.openai_compat.router import openai_compat_enabled
    assert openai_compat_enabled() is False


def test_gate_on(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPAT_API_ENABLED", "true")
    from api.openai_compat.router import openai_compat_enabled
    assert openai_compat_enabled() is True
