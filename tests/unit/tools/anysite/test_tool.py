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
    # `_prepare` now requires BOTH halves — a keyless install used to proceed and
    # fail with the vendor's error instead of ours.
    monkeypatch.setattr("tools.anysite.tool.missing_requirement", lambda: None)
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
    monkeypatch.setattr("tools.anysite.tool.missing_requirement", lambda: "binary")
    res = await tool.anysite_api(AnysiteApiParams(endpoint="/api/x", params=None))
    assert res.error is not None
    assert "anysite" in res.error.lower()


@pytest.mark.asyncio
async def test_anysite_api_surfaces_nonzero_exit(monkeypatch):
    tool = _tool()
    # `_prepare` now requires BOTH halves — a keyless install used to proceed and
    # fail with the vendor's error instead of ours.
    monkeypatch.setattr("tools.anysite.tool.missing_requirement", lambda: None)
    monkeypatch.setattr("tools.anysite.tool.ensure_configured", lambda: True)

    async def fake_run(argv, **kw):
        return AnysiteResult(stdout="", stderr="bad endpoint", exit_code=2, timed_out=False)

    monkeypatch.setattr("tools.anysite.tool.run_anysite", fake_run)
    res = await tool.anysite_api(AnysiteApiParams(endpoint="/api/nope", params=None))
    assert res.error is not None and "bad endpoint" in res.error


@pytest.mark.asyncio
async def test_a_missing_KEY_blocks_the_call_and_says_which_half(monkeypatch):
    """Prod ran for months with the package present and no credential, while the
    tool reported "install with pip install anysite-cli"."""
    tool = _tool()
    monkeypatch.setattr("tools.anysite.tool.missing_requirement", lambda: "key")
    monkeypatch.setattr("tools.anysite.tool.binary_path",
                        lambda: "/opt/polyrob/venv/bin/anysite")

    async def _must_not_run(argv, **kw):
        raise AssertionError("a keyless call must not reach the CLI")

    monkeypatch.setattr("tools.anysite.tool.run_anysite", _must_not_run)
    res = await tool.anysite_api(AnysiteApiParams(endpoint="/api/x", params=None))
    assert res.error is not None
    assert "no credential is set" in res.error
    assert "pip install" not in res.error


# ── anysite_describe (2026-09-15, owner rail "fix Anysite tool") ──────────────

@pytest.mark.asyncio
async def test_describe_search_returns_stdout(monkeypatch):
    tool = _tool()
    monkeypatch.setattr("tools.anysite.tool.missing_requirement", lambda: None)
    monkeypatch.setattr("tools.anysite.tool.ensure_configured", lambda: True)
    seen = {}

    async def fake_run(argv, **kw):
        seen["argv"] = argv
        return AnysiteResult(stdout='["/api/twitter/search/posts"]', stderr="", exit_code=0, timed_out=False)

    monkeypatch.setattr("tools.anysite.tool.run_anysite", fake_run)
    from tools.anysite.tool import AnysiteDescribeParams
    res = await tool.anysite_describe(AnysiteDescribeParams(search="twitter"))
    assert res.error is None
    assert "/api/twitter/search/posts" in res.extracted_content
    assert "--search" in seen["argv"]


@pytest.mark.asyncio
async def test_describe_endpoint_returns_schema(monkeypatch):
    tool = _tool()
    monkeypatch.setattr("tools.anysite.tool.missing_requirement", lambda: None)
    monkeypatch.setattr("tools.anysite.tool.ensure_configured", lambda: True)
    seen = {}

    async def fake_run(argv, **kw):
        seen["argv"] = argv
        return AnysiteResult(stdout='{"input": {"query": {"type": "string"}}}', stderr="", exit_code=0, timed_out=False)

    monkeypatch.setattr("tools.anysite.tool.run_anysite", fake_run)
    from tools.anysite.tool import AnysiteDescribeParams
    res = await tool.anysite_describe(AnysiteDescribeParams(endpoint="/api/twitter/search/posts"))
    assert res.error is None
    assert "query" in res.extracted_content
    assert "/api/twitter/search/posts" in seen["argv"]


@pytest.mark.asyncio
async def test_describe_requires_endpoint_or_search(monkeypatch):
    """Both omitted would dump EVERY endpoint path (~4.5k lines) into context.
    The action must refuse with the two supported shapes instead of running."""
    tool = _tool()
    monkeypatch.setattr("tools.anysite.tool.missing_requirement", lambda: None)
    monkeypatch.setattr("tools.anysite.tool.ensure_configured", lambda: True)

    async def _must_not_run(argv, **kw):
        raise AssertionError("a both-omitted describe must not reach the CLI")

    monkeypatch.setattr("tools.anysite.tool.run_anysite", _must_not_run)
    from tools.anysite.tool import AnysiteDescribeParams
    res = await tool.anysite_describe(AnysiteDescribeParams())
    assert res.error is not None
    assert "search" in res.error and "endpoint" in res.error


@pytest.mark.asyncio
async def test_describe_fails_soft_without_key(monkeypatch):
    tool = _tool()
    monkeypatch.setattr("tools.anysite.tool.missing_requirement", lambda: "key")
    from tools.anysite.tool import AnysiteDescribeParams
    res = await tool.anysite_describe(AnysiteDescribeParams(search="x"))
    assert res.error is not None
    assert "no credential is set" in res.error


def test_anysite_api_description_steers_to_describe():
    """The api action's description must tell the agent to discover the exact
    path + params first — that steering is the fix, the new action is the rail."""
    from tools.anysite.tool import AnysiteTool
    import inspect
    src = inspect.getsource(AnysiteTool.anysite_api)
    assert "anysite_describe" in src


@pytest.mark.asyncio
async def test_empty_search_with_a_long_query_names_the_query_shape(monkeypatch):
    """Rob's evening self-review #5 (2026-09-19): `/api/twitter/search/users` returned
    `[]` for every multi-word phrasing and 20 rows for the same theme in two words —
    a query-shape artefact the agent read as "theme exhausted" and recorded as such.
    An empty search answer on a >2-word query must say so, not look like no supply."""
    tool = _tool()
    monkeypatch.setattr("tools.anysite.tool.missing_requirement", lambda: None)
    monkeypatch.setattr("tools.anysite.tool.ensure_configured", lambda: True)

    async def fake_run(argv, **kw):
        return AnysiteResult(stdout="[]", stderr="", exit_code=0, timed_out=False)

    monkeypatch.setattr("tools.anysite.tool.run_anysite", fake_run)
    res = await tool.anysite_api(AnysiteApiParams(
        endpoint="/api/twitter/search/users",
        params={"query": "indie hacker autonomous agent", "count": 20}))
    assert res.error is None
    out = res.extracted_content
    assert "[]" in out and "query" in out.lower()
    assert "2" in out and ("shorter" in out.lower() or "fewer" in out.lower())
    assert "not" in out.lower() and "exhaust" in out.lower()

    # a two-word query that is genuinely empty gets the bare answer
    res2 = await tool.anysite_api(AnysiteApiParams(
        endpoint="/api/twitter/search/users", params={"query": "indie hacker"}))
    assert res2.extracted_content.strip() == "[]"

    # a non-search endpoint is untouched
    res3 = await tool.anysite_api(AnysiteApiParams(
        endpoint="/api/linkedin/user", params={"user": "a b c d"}))
    assert res3.extracted_content.strip() == "[]"
