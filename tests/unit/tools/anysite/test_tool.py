"""Unit tests for AnysiteTool: anysite_api + anysite_schema_update actions."""
import pytest
from tools.anysite.tool import AnysiteTool, AnysiteApiParams
from tools.anysite.client import AnysiteResult


def _tool():
    """Create an AnysiteTool bypassing BaseTool/BaseComponent __init__."""
    t = object.__new__(AnysiteTool)
    t._configured = False
    return t


@pytest.mark.asyncio
async def test_anysite_api_returns_stdout(monkeypatch):
    tool = _tool()
    monkeypatch.setattr("tools.anysite.tool.binary_available", lambda: True)
    monkeypatch.setattr("tools.anysite.tool.ensure_configured", lambda: True)

    async def fake_run(argv, **kw):
        assert "api" in argv
        return AnysiteResult(stdout='{"name": "Satya"}', stderr="", exit_code=0, timed_out=False)

    monkeypatch.setattr("tools.anysite.tool.run_anysite", fake_run)
    res = await tool.anysite_api(AnysiteApiParams(endpoint="/api/linkedin/user", params={"user": "satyanadella"}))
    assert res.error is None
    assert "Satya" in res.extracted_content


@pytest.mark.asyncio
async def test_anysite_api_fails_soft_without_binary(monkeypatch):
    tool = _tool()
    monkeypatch.setattr("tools.anysite.tool.binary_available", lambda: False)
    res = await tool.anysite_api(AnysiteApiParams(endpoint="/api/x", params=None))
    assert res.error is not None
    assert "anysite" in res.error.lower()


@pytest.mark.asyncio
async def test_anysite_api_surfaces_nonzero_exit(monkeypatch):
    tool = _tool()
    monkeypatch.setattr("tools.anysite.tool.binary_available", lambda: True)
    monkeypatch.setattr("tools.anysite.tool.ensure_configured", lambda: True)

    async def fake_run(argv, **kw):
        return AnysiteResult(stdout="", stderr="bad endpoint", exit_code=2, timed_out=False)

    monkeypatch.setattr("tools.anysite.tool.run_anysite", fake_run)
    res = await tool.anysite_api(AnysiteApiParams(endpoint="/api/nope", params=None))
    assert res.error is not None and "bad endpoint" in res.error
