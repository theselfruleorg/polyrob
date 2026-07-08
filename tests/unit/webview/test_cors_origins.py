"""P0-1 (2026-07-06 UX handoff) — Socket.IO CORS must allow the console's own
serving origin.

The default allowlist only carried localhost:3000 + WEBVIEW_DOMAIN, so a
browser on http://127.0.0.1:{bind_port} (the actual serving origin in
local/dev) had its engineio handshake rejected with a 400 and every live
stream (session feed AND /activity) was dead.

Fix under test:
- the DEFAULT allowlist (CORS_ALLOW_ORIGINS unset) includes the bind-port
  origins on localhost/127.0.0.1;
- the origin gate handed to engineio is a callable that additionally allows
  TRUE same-origin requests (Origin == scheme://Host — Host is a
  browser-forbidden header, so it is browser-attested) while NEVER trusting
  the JS-settable X-Forwarded-* headers for that comparison.
"""
import importlib

import pytest


def _reload_server(monkeypatch, **env):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "false")
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    monkeypatch.delenv("WEBGATE_PORT", raising=False)
    monkeypatch.delenv("WEBVIEW_PORT", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import webview.server as server
    return importlib.reload(server)


def test_default_cors_includes_bind_port_origins(monkeypatch):
    server = _reload_server(monkeypatch, WEBVIEW_PORT="5591")
    for origin in (
        "http://127.0.0.1:5591",
        "http://localhost:5591",
        "https://127.0.0.1:5591",
        "https://localhost:5591",
    ):
        assert origin in server._cors_origins, f"default allowlist missing {origin}"


def test_default_cors_includes_service_port_of_record(monkeypatch):
    """No port env set → bind_port() default (5050) must still be allowed."""
    server = _reload_server(monkeypatch)
    assert "http://127.0.0.1:5050" in server._cors_origins
    assert "http://localhost:5050" in server._cors_origins


def test_default_cors_keeps_legacy_entries(monkeypatch):
    server = _reload_server(monkeypatch, WEBVIEW_DOMAIN="custom.example.com")
    assert "https://custom.example.com" in server._cors_origins
    assert "http://localhost:3000" in server._cors_origins


def test_explicit_cors_env_wins_verbatim(monkeypatch):
    server = _reload_server(monkeypatch)
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://only.example.com")
    server = importlib.reload(server)
    assert server._cors_origins == ["https://only.example.com"]


def test_same_origin_allowed_even_when_not_listed(monkeypatch):
    """A browser served BY this server (any port) must always connect."""
    server = _reload_server(monkeypatch)
    environ = {"HTTP_HOST": "127.0.0.1:9999", "wsgi.url_scheme": "http"}
    assert server._cors_origin_allowed("http://127.0.0.1:9999", environ) is True


def test_listed_origin_allowed_without_host_match(monkeypatch):
    server = _reload_server(monkeypatch, WEBVIEW_DOMAIN="app.example.com")
    environ = {"HTTP_HOST": "127.0.0.1:5050", "wsgi.url_scheme": "http"}
    assert server._cors_origin_allowed("https://app.example.com", environ) is True


def test_cross_origin_denied(monkeypatch):
    server = _reload_server(monkeypatch)
    environ = {"HTTP_HOST": "127.0.0.1:5050", "wsgi.url_scheme": "http"}
    assert server._cors_origin_allowed("https://evil.example", environ) is False


def test_forwarded_host_is_never_trusted(monkeypatch):
    """X-Forwarded-* are settable from cross-origin JS (they are not
    browser-forbidden headers) — the same-origin comparison must use only
    HTTP_HOST, or an attacker page could smuggle its own origin through."""
    server = _reload_server(monkeypatch)
    environ = {
        "HTTP_HOST": "127.0.0.1:5050",
        "wsgi.url_scheme": "http",
        "HTTP_X_FORWARDED_HOST": "evil.example",
        "HTTP_X_FORWARDED_PROTO": "https",
    }
    assert server._cors_origin_allowed("https://evil.example", environ) is False


def test_missing_host_header_denies_unlisted_origin(monkeypatch):
    server = _reload_server(monkeypatch)
    assert server._cors_origin_allowed("https://evil.example", {}) is False


def test_engineio_wired_with_the_callable(monkeypatch):
    server = _reload_server(monkeypatch)
    assert server._sio.eio.cors_allowed_origins is server._cors_origin_allowed
