"""WS-4: narrow loopback allowlist for the agent's own published sandbox ports.

The agent starts a server INSIDE its sandbox container; its ports are published to
host loopback (127.0.0.1:<hostport>). To HTTP-test it, the browser/web_fetch SSRF
guard must permit EXACTLY those loopback host ports — never a blanket private-URL
opening, and never cloud-metadata (169.254.169.254 is NOT loopback).
"""
import pytest

from tools.shell.loopback_allow import (
    allow_loopback_ports, clear_loopback_ports, is_loopback_allowed,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_loopback_ports()
    yield
    clear_loopback_ports()


def test_nothing_allowed_by_default():
    assert is_loopback_allowed("http://127.0.0.1:8000/") is False


def test_allowed_after_registration():
    allow_loopback_ports([18000, 18001])
    assert is_loopback_allowed("http://127.0.0.1:18000/") is True
    assert is_loopback_allowed("http://127.0.0.1:18001/app") is True


def test_localhost_and_ipv6_loopback_also_allowed():
    allow_loopback_ports([18000])
    assert is_loopback_allowed("http://localhost:18000/") is True
    assert is_loopback_allowed("http://[::1]:18000/") is True


def test_unregistered_port_denied():
    allow_loopback_ports([18000])
    assert is_loopback_allowed("http://127.0.0.1:9999/") is False


def test_non_loopback_host_never_allowed_even_on_allowed_port():
    allow_loopback_ports([18000])
    # a public host on the same port number is NOT loopback -> denied
    assert is_loopback_allowed("http://93.184.216.34:18000/") is False
    assert is_loopback_allowed("http://example.com:18000/") is False


def test_cloud_metadata_never_allowed():
    allow_loopback_ports([80, 18000])
    # 169.254.169.254 is link-local, NOT loopback — must never be allowed here
    assert is_loopback_allowed("http://169.254.169.254/latest/meta-data/") is False
    assert is_loopback_allowed("http://169.254.169.254:18000/") is False


def test_private_rfc1918_never_allowed():
    allow_loopback_ports([18000])
    assert is_loopback_allowed("http://10.0.0.5:18000/") is False
    assert is_loopback_allowed("http://192.168.1.1:18000/") is False


def test_clear_revokes():
    allow_loopback_ports([18000])
    clear_loopback_ports()
    assert is_loopback_allowed("http://127.0.0.1:18000/") is False


def test_revoke_removes_only_named_ports():
    # session teardown revokes exactly that container's ports, not the whole allowlist
    from tools.shell.loopback_allow import revoke_loopback_ports
    allow_loopback_ports([18000, 18001, 18002])
    revoke_loopback_ports([18000, 18002])
    assert is_loopback_allowed("http://127.0.0.1:18000/") is False
    assert is_loopback_allowed("http://127.0.0.1:18002/") is False
    assert is_loopback_allowed("http://127.0.0.1:18001/") is True  # untouched


def test_garbage_url_denied():
    allow_loopback_ports([18000])
    assert is_loopback_allowed("not a url") is False
    assert is_loopback_allowed("") is False
    assert is_loopback_allowed("ftp://127.0.0.1:18000/") is False  # scheme not http(s)
