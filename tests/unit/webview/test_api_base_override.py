"""POLYROB_API_BASE — the :9000 proxy target is configurable (review W5).

The webview proxies chat delivery / queue-status / health to the main task API
with a hardcoded ``http://127.0.0.1:9000`` at three sites. One helper
(``server._api_base``) + one env override; the literal survives only as the
helper's default.
"""
from pathlib import Path


def test_default_is_loopback_9000(monkeypatch):
    import webview.server as server
    monkeypatch.delenv("POLYROB_API_BASE", raising=False)
    assert server._api_base() == "http://127.0.0.1:9000"


def test_env_override_wins_and_strips_trailing_slash(monkeypatch):
    import webview.server as server
    monkeypatch.setenv("POLYROB_API_BASE", "http://10.0.0.5:9100/")
    assert server._api_base() == "http://10.0.0.5:9100"


def test_the_literal_survives_only_in_the_helper():
    import webview.server as server
    src = Path(server.__file__).read_text()
    assert src.count("http://127.0.0.1:9000") == 1
