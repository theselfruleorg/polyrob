"""F-1 characterization: pin ``utils.rate_limit_manager.RateLimitManager``'s
public contract BEFORE consolidating its internals onto
``core.rate_limit.SlidingWindowLimiter``.

Public surface only: ``check_rate_limit`` (allow/deny/raise/init-mode),
``get_remaining_requests``, ``get_reset_time``. The clock is patched globally
(``time.time``) — both the legacy inline timestamp lists and the core primitive
read it at call time.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.exceptions import RateLimitError
from utils.rate_limit_manager import RateLimitManager


def _mgr():
    cfg = SimpleNamespace(rate_limit_window=60, rate_limit_max_requests=100, cache_ttl=3600)
    return RateLimitManager(name="rate_limit_manager_test", config=cfg)


@pytest.mark.asyncio
async def test_allows_under_limit_then_raises_over():
    m = _mgr()
    with patch("time.time", return_value=1000.0):
        for _ in range(3):
            assert await m.check_rate_limit("op-a", max_requests=3, time_window=60)
        with pytest.raises(RateLimitError):
            await m.check_rate_limit("op-a", max_requests=3, time_window=60)


@pytest.mark.asyncio
async def test_raise_on_limit_false_returns_false():
    m = _mgr()
    with patch("time.time", return_value=1000.0):
        assert await m.check_rate_limit("op-b", max_requests=1, time_window=60)
        result = await m.check_rate_limit(
            "op-b", max_requests=1, time_window=60, raise_on_limit=False
        )
    assert result is False


@pytest.mark.asyncio
async def test_window_expiry_frees_slots():
    m = _mgr()
    with patch("time.time", return_value=1000.0):
        assert await m.check_rate_limit("op-c", max_requests=1, time_window=60)
        assert not await m.check_rate_limit(
            "op-c", max_requests=1, time_window=60, raise_on_limit=False
        )
    with patch("time.time", return_value=1061.0):
        assert await m.check_rate_limit("op-c", max_requests=1, time_window=60)


@pytest.mark.asyncio
async def test_initialization_mode_bypasses_limit():
    m = _mgr()
    await m.set_initialization_mode(True)
    with patch("time.time", return_value=1000.0):
        for _ in range(5):
            assert await m.check_rate_limit("op-d", max_requests=2, time_window=60)


@pytest.mark.asyncio
async def test_per_operation_isolation():
    m = _mgr()
    with patch("time.time", return_value=1000.0):
        assert await m.check_rate_limit("op-iso-1", max_requests=1, time_window=60)
        assert not await m.check_rate_limit(
            "op-iso-1", max_requests=1, time_window=60, raise_on_limit=False
        )
        assert await m.check_rate_limit("op-iso-2", max_requests=1, time_window=60)


@pytest.mark.asyncio
async def test_get_remaining_requests():
    m = _mgr()
    assert m.get_remaining_requests("never-seen") == -1
    with patch("time.time", return_value=1000.0):
        await m.check_rate_limit("op-e", max_requests=5, time_window=60)
        await m.check_rate_limit("op-e", max_requests=5, time_window=60)
        assert m.get_remaining_requests("op-e") == 3


@pytest.mark.asyncio
async def test_get_reset_time():
    m = _mgr()
    assert m.get_reset_time("never-seen") == -1
    with patch("time.time", return_value=1000.0):
        await m.check_rate_limit("op-f", max_requests=5, time_window=60)
    with patch("time.time", return_value=1010.0):
        assert m.get_reset_time("op-f") == 50.0
