"""config_service surface guard + dynamic-pattern flag writes (UX assessment
2026-08-07, Q9/Q11).

The credential-surface refusal must live in the ORACLE (`set_value`), not only
at the webview call site — otherwise the first surface that grows a flag-write
path (Telegram parity, a future API) silently bypasses it.
"""
from core import config_service


def test_console_surface_refuses_credential_flags():
    for key in ("LLM_CUSTOM_PROVIDERS", "POLYROB_AUTH_STORE", "LLM_AUTH_STORE_ENABLED",
                "LLM_CREDENTIAL_BORROW", "MEMORY_SCOPES_ENABLED"):
        res = config_service.set_value(key, "x", surface="console")
        assert res.ok is False, key
        assert "local CLI" in res.message, key


def test_local_surface_still_writes_credential_flags(monkeypatch):
    sentinel = config_service.SetResult(True, "written", "ok")
    monkeypatch.setattr(config_service, "_set_flag", lambda *a, **k: sentinel)
    res = config_service.set_value("LLM_CUSTOM_PROVIDERS", "/tmp/x.yaml")
    assert res is sentinel


def test_pattern_flag_key_routes_to_flag_writer(monkeypatch):
    """AUX_MODEL_<SLOT> is a documented dynamic pattern — a concrete
    instance must route to the flag writer, not 'unknown key'."""
    sentinel = config_service.SetResult(True, "written", "ok")
    monkeypatch.setattr(config_service, "_set_flag", lambda *a, **k: sentinel)
    res = config_service.set_value("AUX_MODEL_JUDGE", "x-ai/grok-4.3")
    assert res is sentinel


def test_pattern_flag_registered_in_catalog():
    from core.flags import PATTERNS
    assert any(f.name == "AUX_MODEL_<SLOT>" for f in PATTERNS)
    assert not any(f.name == "POLYROB_<PROVIDER>_MODEL" for f in PATTERNS)  # retired


def test_unknown_key_still_refused():
    res = config_service.set_value("TOTALLY_MADE_UP_XYZ", "1")
    assert res.ok is False
    assert "unknown key" in res.message
