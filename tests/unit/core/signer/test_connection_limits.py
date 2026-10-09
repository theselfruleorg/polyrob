"""Idle or stalled clients cannot allocate unbounded signing workers."""
from types import SimpleNamespace
from unittest.mock import Mock

from core.signer.server import SignerServer


def test_connection_flood_is_bounded_before_thread_creation(monkeypatch):
    server = SignerServer(Mock(), "/unused")
    connections = [Mock() for _ in range(18)]
    pending = iter(connections)
    def accept():
        connection = next(pending)
        if connection is connections[-1]:
            server._stop.set()
        return connection, None
    server._sock = SimpleNamespace(accept=accept)
    threads = []
    def thread(**kwargs):
        threads.append(kwargs)
        return SimpleNamespace(start=lambda: None)
    monkeypatch.setattr("core.signer.server.threading.Thread", thread)
    server.serve_forever()
    assert len(threads) == 16
    for connection in connections[16:]:
        connection.close.assert_called_once()
    server._serve_conn = Mock(side_effect=RuntimeError("failed handler"))
    import pytest
    with pytest.raises(RuntimeError):
        server._serve_bounded(connections[0])
    assert server._connections.acquire(blocking=False)
