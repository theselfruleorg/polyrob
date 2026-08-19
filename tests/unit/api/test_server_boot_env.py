"""Env-scheme unification (2026-08-14): api.server_boot must not be a fourth,
divergent env loader. Its pre-load delegates to core.bootstrap.load_env — the
ONE layering SSOT (honors CONFIG_ENV, full candidate order) — instead of the
old single-file `config/.env.{ENV}` XOR root-.env read."""


def test_server_boot_load_env_delegates_to_bootstrap(monkeypatch):
    calls = {}
    monkeypatch.setattr("core.bootstrap.load_env",
                        lambda *a, **k: calls.setdefault("called", True))
    from api.server_boot import _load_env
    _load_env()
    assert calls.get("called"), "_load_env must route through core.bootstrap.load_env"
