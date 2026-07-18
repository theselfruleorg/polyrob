"""Regression (P1 finalization): local posture has NO auth (loopback operator IS the
owner), so an explicit WEBGATE_HOST=0.0.0.0 override would expose an unauthenticated
console to the network. In local posture a non-loopback bind must be refused (forced
to loopback). Authenticated postures (own_ops/multitenant) may bind non-loopback.
"""
import importlib

import pytest


def _webgate(monkeypatch, posture, host=None):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", posture)
    if host is None:
        monkeypatch.delenv("WEBGATE_HOST", raising=False)
        monkeypatch.delenv("WEBVIEW_HOST", raising=False)
    else:
        monkeypatch.setenv("WEBGATE_HOST", host)
    import webview.webgate as wg
    return importlib.reload(wg)


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    monkeypatch.delenv("WEBGATE_HOST", raising=False)
    import webview.webgate as wg
    importlib.reload(wg)


def test_local_non_loopback_override_forced_to_loopback(monkeypatch):
    wg = _webgate(monkeypatch, "local", host="0.0.0.0")
    assert wg.bind_host() == "127.0.0.1"


def test_local_loopback_override_honored(monkeypatch):
    wg = _webgate(monkeypatch, "local", host="127.0.0.1")
    assert wg.bind_host() == "127.0.0.1"


def test_local_default_is_loopback(monkeypatch):
    wg = _webgate(monkeypatch, "local")
    assert wg.bind_host() == "127.0.0.1"


def test_multitenant_may_bind_non_loopback(monkeypatch):
    wg = _webgate(monkeypatch, "multitenant", host="0.0.0.0")
    assert wg.bind_host() == "0.0.0.0"
