"""Local validation data is privileged and bounded, including evidence payloads."""
from types import SimpleNamespace

import pytest

from modules.eip8004 import validation

ADDRESS = "0x" + "2" * 40


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(validation, "get_eip8004_config", lambda: SimpleNamespace(agent_id=42))
    return validation.ValidationManager()


@pytest.mark.asyncio
async def test_request_capacity_and_bytes(manager):
    manager.MAX_REQUESTS = 2
    for n in range(2):
        await manager.request_validation(ADDRESS, {"n": n})
    await manager.request_validation(ADDRESS, {"n": 0})  # retry uses the same slot
    with pytest.raises(ValueError, match="capacity"):
        await manager.request_validation(ADDRESS, {"n": 2})
    with pytest.raises(ValueError, match="64 KiB"):
        await manager.request_validation(ADDRESS, {"data": "x" * 65536})
    assert len(manager._requests) == 2


@pytest.mark.asyncio
async def test_response_evidence_and_count_bounded(manager):
    manager.MAX_RESPONSES_PER_REQUEST = 2
    request = await manager.request_validation(ADDRESS, {"n": 0})
    key = request["requestHash"]
    with pytest.raises(ValueError, match="64 KiB"):
        await manager.submit_response(key, 100, response_data={"data": "x" * 65536})
    with pytest.raises(ValueError, match="32 bytes"):
        await manager.submit_response(key, 100, tag="x" * 33)
    for _ in range(2):
        await manager.submit_response(key, 100, tag="ok")
    with pytest.raises(ValueError, match="capacity"):
        await manager.submit_response(key, 100)
    assert len(manager._responses[key]) == 2
