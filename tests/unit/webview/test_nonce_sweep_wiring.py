"""E2 — the nonce sweep must actually be wired to run periodically, not just exist."""
import pytest


@pytest.mark.asyncio
async def test_sweep_once_invokes_cleanup_expired_nonces():
    import webview.server as server

    calls = []

    class _FakeAuth:
        async def cleanup_expired_nonces(self):
            calls.append(1)

    await server._sweep_expired_nonces_once(_FakeAuth())
    assert calls == [1]


@pytest.mark.asyncio
async def test_sweep_once_is_fail_open_on_error():
    """A sweep failure must never propagate — it would otherwise be able to
    crash the startup background task."""
    import webview.server as server

    class _BrokenAuth:
        async def cleanup_expired_nonces(self):
            raise RuntimeError("db unavailable")

    await server._sweep_expired_nonces_once(_BrokenAuth())  # must not raise
