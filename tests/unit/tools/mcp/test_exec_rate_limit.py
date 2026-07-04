"""WS-B3 — per-(user, server) MCP tool-execution rate limiter.

Closes the gap where per-user limits guarded add_server but NOT tool execution.
Uses an injected clock — no real sleeps.
"""
from tools.mcp.rate_limit import MCPExecRateLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_allows_up_to_limit_then_blocks():
    clk = FakeClock()
    rl = MCPExecRateLimiter(max_calls=3, window_seconds=60, time_fn=clk)
    key = ("user1", "anysite")
    assert rl.check(key) is True
    assert rl.check(key) is True
    assert rl.check(key) is True
    assert rl.check(key) is False  # 4th within window blocked


def test_window_slides_and_frees_slots():
    clk = FakeClock()
    rl = MCPExecRateLimiter(max_calls=2, window_seconds=60, time_fn=clk)
    key = ("u", "s")
    assert rl.check(key) is True
    assert rl.check(key) is True
    assert rl.check(key) is False
    clk.advance(61)  # window passed
    assert rl.check(key) is True


def test_keys_are_isolated_per_user_and_server():
    clk = FakeClock()
    rl = MCPExecRateLimiter(max_calls=1, window_seconds=60, time_fn=clk)
    assert rl.check(("userA", "s")) is True
    assert rl.check(("userA", "s")) is False
    # different user — own bucket
    assert rl.check(("userB", "s")) is True
    # same user, different server — own bucket
    assert rl.check(("userA", "other")) is True


def test_retry_after_reports_seconds_until_slot():
    clk = FakeClock()
    rl = MCPExecRateLimiter(max_calls=1, window_seconds=60, time_fn=clk)
    key = ("u", "s")
    rl.check(key)
    assert rl.check(key) is False
    # oldest call was at t=1000, window 60 -> frees at 1060, now 1000 -> ~60
    assert 59 <= rl.retry_after(key) <= 60
