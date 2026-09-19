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


def test_server_boot_trusts_only_the_local_proxy_by_default(monkeypatch):
    """`forwarded_allow_ips="*"` let any direct caller claim `127.0.0.1` via
    X-Forwarded-For on the `polyrob serve` path the x402 unit runs. Default to the
    local nginx only (the same pin webview/server_launcher.py carries); an operator
    with a remote proxy names it via UVICORN_FORWARDED_ALLOW_IPS."""
    import uvicorn
    import api.server_boot as sb
    captured = {}
    monkeypatch.setattr(sb, "_load_env", lambda: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: captured.update(kw))
    monkeypatch.delenv("UVICORN_FORWARDED_ALLOW_IPS", raising=False)
    sb.run_server(host="127.0.0.1", port=1, workers=1, reload=False, log_level="info")
    assert captured["forwarded_allow_ips"] == "127.0.0.1"
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", "10.0.0.5")
    sb.run_server(host="127.0.0.1", port=1, workers=1, reload=False, log_level="info")
    assert captured["forwarded_allow_ips"] == "10.0.0.5"
