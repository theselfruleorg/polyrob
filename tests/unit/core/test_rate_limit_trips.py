"""045 lane 4: a NAMED limiter's trip records one `rate_limited` row per
(limiter, key) per window; an unnamed limiter records nothing; a raising log
never changes the decision."""
import pytest


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "true")
    import core.event_log as el
    import core.rate_limit as rl
    import core.surfaces.access_log as al
    el._INSTANCES.clear()
    rl._TRIP_LAST.clear()
    monkeypatch.setattr(al, "_OWNER_TENANT", "owner-1")
    yield el
    el._INSTANCES.clear()
    rl._TRIP_LAST.clear()


def _trips(el):
    from core.event_kinds import RATE_LIMITED
    return el.get_event_log().query(kind=RATE_LIMITED)


def test_named_limiter_records_one_row_per_window(log):
    from core.rate_limit import SlidingWindowLimiter
    clock = [1000.0]
    lim = SlidingWindowLimiter(1, 60, time_fn=lambda: clock[0], name="webview_connect")
    assert lim.check("1.2.3.4") is True
    for _ in range(50):
        assert lim.check("1.2.3.4") is False
    rows = _trips(log)
    assert len(rows) == 1
    assert rows[0]["attrs"]["limiter"] == "webview_connect"
    assert rows[0]["attrs"]["key"] == "1.2.3.4"
    assert rows[0]["user_id"] == "owner-1"   # the DEPLOYMENT tenant, like lane 1
    clock[0] += 61
    assert lim.check("1.2.3.4") is True
    assert lim.check("1.2.3.4") is False
    assert len(_trips(log)) == 2


def test_distinct_keys_are_distinct_episodes(log):
    from core.rate_limit import SlidingWindowLimiter
    lim = SlidingWindowLimiter(0, 60, name="x402_public")
    lim.check("a")
    lim.check("b")
    assert sorted(r["attrs"]["key"] for r in _trips(log)) == ["a", "b"]


def test_unnamed_limiter_records_nothing(log):
    from core.rate_limit import SlidingWindowLimiter
    lim = SlidingWindowLimiter(0, 60)
    assert lim.check("k") is False
    assert _trips(log) == []


def test_key_is_truncated(log):
    from core.rate_limit import report_trip
    assert report_trip("api_minute", "x" * 500) is True
    assert len(_trips(log)[0]["attrs"]["key"]) == 64


def test_disabled_security_log_records_nothing(log, monkeypatch):
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "false")
    from core.rate_limit import report_trip
    assert report_trip("api_minute", "k") is False
    assert _trips(log) == []


def test_raising_log_never_changes_the_decision(log, monkeypatch):
    import core.event_log as el
    from core.rate_limit import SlidingWindowLimiter
    monkeypatch.setattr(el, "get_event_log",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    lim = SlidingWindowLimiter(1, 60, name="webview_event")
    assert lim.check("s") is True
    assert lim.check("s") is False


def test_throttle_state_is_bounded(log, monkeypatch):
    import core.rate_limit as rl
    monkeypatch.setattr(rl, "_TRIP_MAX_KEYS", 3)
    for k in "abcdef":
        rl.report_trip("t", k)
    assert len(rl._TRIP_LAST) == 3


def test_api_middleware_trip_is_recorded(log):
    from api.middleware import RateLimiter
    r = RateLimiter(requests_per_minute=1, requests_per_hour=100, burst_size=5)
    assert r.check_rate_limit("caller")[0] is True
    allowed, info = r.check_rate_limit("caller")
    assert allowed is False and info.window == "minute"
    rows = _trips(log)
    assert [x["attrs"]["limiter"] for x in rows] == ["api_minute"]
