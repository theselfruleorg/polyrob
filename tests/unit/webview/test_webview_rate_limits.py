"""F-1 characterization: pin the webview module-level rate-limit functions
(``check_rate_limit`` per-IP connection throttle, ``check_event_rate_limit``
per-session event throttle) BEFORE consolidating them onto ``core.rate_limit``.

Assertions go through the two public functions only. Keys are unique per test
(uuid) so the module-level limiter state never needs resetting — which also keeps
the tests valid across the consolidation. The clock is controlled by patching
``time.time`` globally (both the legacy inline lists and the core primitive read
it at call time).
"""
import uuid
from unittest.mock import patch

from webview import server


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def test_connection_limit_allows_up_to_max_then_denies():
    ip = _key("ip")
    with patch("time.time", return_value=1000.0):
        for i in range(server.RATE_LIMIT_MAX_CONNECTIONS):
            assert server.check_rate_limit(ip), f"connection {i} should be allowed"
        assert not server.check_rate_limit(ip)


def test_connection_limit_isolates_ips():
    ip1, ip2 = _key("ip"), _key("ip")
    with patch("time.time", return_value=1000.0):
        for _ in range(server.RATE_LIMIT_MAX_CONNECTIONS):
            server.check_rate_limit(ip1)
        assert not server.check_rate_limit(ip1)
        assert server.check_rate_limit(ip2)


def test_connection_limit_window_expiry_reallows():
    ip = _key("ip")
    with patch("time.time", return_value=1000.0):
        for _ in range(server.RATE_LIMIT_MAX_CONNECTIONS):
            server.check_rate_limit(ip)
        assert not server.check_rate_limit(ip)
    with patch("time.time", return_value=1000.0 + server.RATE_LIMIT_WINDOW + 1):
        assert server.check_rate_limit(ip)


def test_event_limit_allows_up_to_max_then_denies():
    sid = _key("sess")
    with patch("time.time", return_value=1000.0):
        for i in range(server.RATE_LIMIT_MAX_EVENTS):
            assert server.check_event_rate_limit(sid), f"event {i} should be allowed"
        assert not server.check_event_rate_limit(sid)


def test_event_limit_isolates_sessions():
    sid1, sid2 = _key("sess"), _key("sess")
    with patch("time.time", return_value=1000.0):
        for _ in range(server.RATE_LIMIT_MAX_EVENTS):
            server.check_event_rate_limit(sid1)
        assert not server.check_event_rate_limit(sid1)
        assert server.check_event_rate_limit(sid2)


def test_event_limit_window_expiry_reallows():
    sid = _key("sess")
    with patch("time.time", return_value=1000.0):
        for _ in range(server.RATE_LIMIT_MAX_EVENTS):
            server.check_event_rate_limit(sid)
        assert not server.check_event_rate_limit(sid)
    with patch("time.time", return_value=1000.0 + server.RATE_LIMIT_WINDOW + 1):
        assert server.check_event_rate_limit(sid)
