"""core/security/browser_rail.py — the ONE answer to "can I drive a browser?".

Pins the three states, that the refusal sentence names the REAL state (not a
fixed "configure a remote browser" while one is configured), that endpoint
values never appear in any rendering, and that the status snapshot's
``probe=False`` read never opens a socket to a non-loopback host.
"""
import socket
import urllib.error

import pytest

from core.security import browser_rail as br


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("BROWSER_CDP_URL", "BROWSER_WSS_URL", "AGENT_WALLET_ENABLED",
                 "AGENT_WALLET_MASTER_SEED", "PAYMENT_MASTER_SEED", "MASTER_SEED"):
        monkeypatch.delenv(name, raising=False)
    br.reset_cache()
    yield
    br.reset_cache()


def test_no_custody_no_endpoint_is_local_ok():
    st = br.browser_rail_status()
    assert st.state == "ok" and st.endpoint == "local" and st.usable
    assert st.refusal() == ""
    assert st.line() == "local Chromium"


def test_custody_without_endpoint_is_unset_with_install_remedy(monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    st = br.browser_rail_status()
    assert st.state == "unset" and not st.usable
    assert "no remote browser endpoint is configured" in st.refusal()
    assert "polyrob browser install" in st.refusal()
    assert "BROWSER_CDP_URL" in st.refusal()


def test_configured_and_answering_is_ok_with_version(monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(br, "_probe_cdp", lambda url: (True, "", "Chrome/148.0"))
    st = br.browser_rail_status()
    assert st.ok and st.endpoint == "cdp" and st.version == "Chrome/148.0"
    assert st.line() == "remote cdp ok (Chrome/148.0)"


def test_configured_but_refused_is_unreachable_with_service_remedy(monkeypatch):
    """The 2026-09-17 misdiagnosis: the sentence must say CONFIGURED, not 'configure one'."""
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(br, "_probe_cdp", lambda url: (False, "connection refused", ""))
    st = br.browser_rail_status()
    assert st.state == "unreachable" and not st.usable
    assert "configured but not reachable (connection refused)" in st.refusal()
    assert "polyrob-browser.service" in st.refusal()
    assert "Configure a separately isolated" not in st.refusal()


def test_endpoint_value_never_rendered(monkeypatch):
    secret = "http://user:tok3n-secret@127.0.0.1:9222/?token=tok3n-secret"
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("BROWSER_CDP_URL", secret)
    monkeypatch.setattr(br, "_probe_cdp",
                        lambda url: (False, br._reason_class(ConnectionRefusedError(secret)), ""))
    st = br.browser_rail_status()
    for text in (st.refusal(), st.line(), st.reason, st.remedy, repr(st)):
        assert "tok3n-secret" not in text


def test_reason_is_a_class_never_a_message():
    assert br._reason_class(ConnectionRefusedError("x http://secret")) == "connection refused"
    assert br._reason_class(socket.timeout("http://secret")) == "timeout"
    assert br._reason_class(socket.gaierror("http://secret")) == "dns failure"
    assert "secret" not in br._reason_class(RuntimeError("http://secret"))


def test_probe_false_never_touches_a_remote_host(monkeypatch):
    monkeypatch.setenv("BROWSER_WSS_URL", "wss://browser.example.test:3000/t")
    monkeypatch.setattr(br, "_probe_tcp",
                        lambda url: (_ for _ in ()).throw(AssertionError("must not probe")))
    st = br.browser_rail_status(probe=False)
    assert st.state == "configured" and st.usable
    assert st.line() == "remote wss configured (not probed)"
    # and it is NOT cached as an answer — the next probing read still probes
    monkeypatch.setattr(br, "_probe_tcp", lambda url: (True, "", ""))
    assert br.browser_rail_status().ok


def test_probe_false_still_probes_loopback(monkeypatch):
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    calls = []
    monkeypatch.setattr(br, "_probe_cdp", lambda url: (calls.append(url), (True, "", "v"))[1])
    assert br.browser_rail_status(probe=False).ok
    assert calls == ["http://127.0.0.1:9222"]


def test_cache_ttl_and_refresh(monkeypatch):
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    n = {"calls": 0}

    def probe(url):
        n["calls"] += 1
        return (True, "", "")
    monkeypatch.setattr(br, "_probe_cdp", probe)
    br.browser_rail_status(); br.browser_rail_status()
    assert n["calls"] == 1
    br.browser_rail_status(refresh=True)
    assert n["calls"] == 2


def test_cdp_probe_maps_urlerror_to_class(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError(ConnectionRefusedError("http://secret"))
    monkeypatch.setattr("urllib.request.urlopen", boom)
    ok, reason, _ = br._probe_cdp("http://127.0.0.1:9222")
    assert not ok and reason == "connection refused"
