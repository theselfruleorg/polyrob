"""043 W6 — the owner-login throttle evicts, it does not amnesty.

`_record_login_attempt` bounded its memory with `_login_attempts.clear()` above
10 000 IPs: one address-churning attacker (trivially cheap over IPv6, where a
single /64 hands out billions) wiped the attempt history of EVERY honest IP,
including its own. The bound is right; wholesale forgetting is not. An
`OrderedDict` of the 10 000 most recently seen IPs evicts the OLDEST entry
instead, so a flood loses its own oldest records and never the live ones.
"""
import importlib

import pytest


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("ENV", "development")
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    yield srv
    srv._login_attempts.clear()


def test_the_bound_still_holds(server):
    for i in range(server._LOGIN_ATTEMPT_LRU_MAX + 500):
        server._record_login_attempt(f"10.0.{i // 256}.{i % 256}")
    assert len(server._login_attempts) == server._LOGIN_ATTEMPT_LRU_MAX


def test_the_oldest_ip_is_evicted_not_everyone(server):
    server._record_login_attempt("1.1.1.1")
    for i in range(server._LOGIN_ATTEMPT_LRU_MAX):
        server._record_login_attempt(f"10.0.{i // 256}.{i % 256}")
    assert "1.1.1.1" not in server._login_attempts          # the oldest went
    assert "10.0.0.1" in server._login_attempts             # an honest neighbour stayed


def test_a_flood_cannot_amnesty_an_attacker_at_the_limit(server):
    """The regression that matters: five failed attempts, then a flood, and the
    throttled IP must STILL be throttled if it is still in the window."""
    for _ in range(server._LOGIN_ATTEMPT_MAX):
        server._record_login_attempt("9.9.9.9")
    assert server._login_throttled("9.9.9.9")
    for i in range(server._LOGIN_ATTEMPT_LRU_MAX - 1):
        server._record_login_attempt(f"10.0.{i // 256}.{i % 256}")
    assert server._login_throttled("9.9.9.9"), (
        "an address flood must not clear the record of a throttled IP")


def test_recording_refreshes_recency(server):
    server._record_login_attempt("2.2.2.2")
    for i in range(server._LOGIN_ATTEMPT_LRU_MAX - 1):
        server._record_login_attempt(f"10.0.{i // 256}.{i % 256}")
    server._record_login_attempt("2.2.2.2")                  # seen again → youngest
    server._record_login_attempt("3.3.3.3")                  # forces one eviction
    assert "2.2.2.2" in server._login_attempts


def test_an_unknown_ip_is_not_throttled(server):
    assert not server._login_throttled("4.4.4.4")
