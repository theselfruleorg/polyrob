from datetime import datetime, timedelta

from api.a2a.client import A2AAgentInfo, A2AClient, A2AClientConfig


def test_a2a_client_cache_ttl_marks_old_entries_stale():
    client = A2AClient(A2AClientConfig(cache_ttl=10))
    info = A2AAgentInfo(
        url="https://agent.example",
        card=object(),
        discovered_at=(datetime.now() - timedelta(seconds=11)).isoformat(),
    )

    assert client._is_cache_stale(info) is True


def test_a2a_client_cache_ttl_keeps_fresh_entries():
    client = A2AClient(A2AClientConfig(cache_ttl=10))
    info = A2AAgentInfo(
        url="https://agent.example",
        card=object(),
        discovered_at=datetime.now().isoformat(),
    )

    assert client._is_cache_stale(info) is False


def test_a2a_client_cache_ttl_treats_bad_timestamp_as_stale():
    client = A2AClient(A2AClientConfig(cache_ttl=10))
    info = A2AAgentInfo(
        url="https://agent.example",
        card=object(),
        discovered_at="not-a-date",
    )

    assert client._is_cache_stale(info) is True
