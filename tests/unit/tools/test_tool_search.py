"""Tier-3 item 1 — query-based tool discovery (tool_search / tool_describe).

Pure logic tests (ranking, status honesty, MCP coverage, cap disclosure, describe
fidelity, case-fold, leaf safety). The registered-action wiring is covered in
tests/unit/tools/controller/test_tool_search_action.py.
"""
import asyncio

from tools.tool_search import (
    ToolHit,
    describe_tool,
    perform_tool_describe,
    perform_tool_search,
    search_tools,
)


class FakeContainer:
    def __init__(self, services=()):
        self._s = set(services)

    def has_service(self, n):
        return n in self._s


def _mcp(*rows):
    """rows: (server, canonical_name, server_tool_name, description[, input_schema])."""
    out = []
    for r in rows:
        srv, n, stn, d = r[0], r[1], r[2], r[3]
        sch = r[4] if len(r) > 4 else {}
        out.append({"server": srv, "name": n, "server_tool_name": stn,
                    "description": d, "input_schema": sch})
    return out


class FakeController:
    def __init__(self, services=(), loaded=(), is_sub=False, mcp=(), registry=None):
        self.container = FakeContainer(services)
        self._loaded = list(loaded)
        self._is_sub_agent = is_sub
        self._mcp = list(mcp)
        self.registry = registry

    def list_tools(self):
        return list(self._loaded)

    def iter_mcp_tools_metadata(self):
        return list(self._mcp)


# --- search_tools ranking + status honesty -----------------------------------

def test_ranking_is_deterministic_and_id_exact_wins():
    sr = search_tools("web_fetch", container=FakeContainer({"web_fetch"}),
                      loaded_ids=set(), is_leaf=False, mcp_tools=[], limit=8)
    assert sr.results[0].id == "web_fetch"


def test_ranking_stable_across_calls():
    args = dict(container=FakeContainer(), loaded_ids=set(), is_leaf=False,
                mcp_tools=_mcp(("a", "a_x", "x", "search files"),
                               ("b", "b_x", "x", "search files")), limit=8)
    a = [h.id for h in search_tools("search", **args).results]
    b = [h.id for h in search_tools("search", **args).results]
    assert a == b


def test_money_tool_searchable_but_status_money_gated():
    sr = search_tools("hyperliquid", container=FakeContainer({"hyperliquid"}),
                      loaded_ids=set(), is_leaf=False, mcp_tools=[], limit=8)
    h = next(x for x in sr.results if x.id == "hyperliquid")
    assert h.status == "gated" and h.reason == "money" and h.remedy


def test_case_folded_money_id_is_money_not_unknown():
    sr = search_tools("X402_PAY", container=FakeContainer(), loaded_ids=set(),
                      is_leaf=False, mcp_tools=[], limit=8)
    h = next(x for x in sr.results if x.id == "x402_pay")
    assert h.status == "gated" and h.reason == "money"


def test_delegate_blocked_id_shows_leaf_blocked_for_leaf():
    sr = search_tools("cronjob", container=FakeContainer({"cronjob"}),
                      loaded_ids=set(), is_leaf=True, mcp_tools=[], limit=8)
    h = next(x for x in sr.results if x.id == "cronjob")
    assert h.status == "gated" and h.reason == "leaf-blocked"


def test_loaded_status_reflected():
    sr = search_tools("filesystem", container=FakeContainer({"filesystem"}),
                      loaded_ids={"filesystem"}, is_leaf=False, mcp_tools=[], limit=8)
    h = next(x for x in sr.results if x.id == "filesystem")
    assert h.status == "loaded"


# --- MCP coverage ------------------------------------------------------------

def test_mcp_tools_covered_multi_server_no_reprefix():
    mcp = _mcp(("anysite", "anysite_search", "search", "web search"),
               ("gh", "gh_search", "search", "code search"))
    sr = search_tools("search", container=FakeContainer(), loaded_ids=set(),
                      is_leaf=False, mcp_tools=mcp, limit=8)
    ids = {(h.id, h.where) for h in sr.results}
    assert ("anysite_search", "mcp:anysite") in ids
    assert ("gh_search", "mcp:gh") in ids


def test_mcp_hit_carries_invoke_remedy():
    mcp = _mcp(("anysite", "anysite_search", "search", "web search"))
    sr = search_tools("anysite", container=FakeContainer(), loaded_ids=set(),
                      is_leaf=False, mcp_tools=mcp, limit=8)
    h = next(x for x in sr.results if x.where == "mcp:anysite")
    assert "mcp_execute_tool" in h.remedy and "search" in h.remedy


def test_total_matched_preserved_for_cap_disclosure():
    mcp = _mcp(*[("s%d" % i, "s%d_search" % i, "search", "d") for i in range(20)])
    sr = search_tools("search", container=FakeContainer(), loaded_ids=set(),
                      is_leaf=False, mcp_tools=mcp, limit=3)
    assert len(sr.results) == 3 and sr.total_matched >= 20


def test_no_match_returns_empty():
    sr = search_tools("zzzznotathing", container=FakeContainer(), loaded_ids=set(),
                      is_leaf=False, mcp_tools=[], limit=8)
    assert sr.results == [] and sr.total_matched == 0


# --- perform_tool_search (ActionResult wrapper) ------------------------------

def test_perform_search_lists_and_discloses_cap():
    mcp = _mcp(*[("s%d" % i, "s%d_go" % i, "go", "navigate") for i in range(12)])
    c = FakeController(mcp=mcp)
    res = asyncio.run(perform_tool_search(c, "go", 3))
    body = res.extracted_content
    assert body.count("\n- ") <= 3
    assert "showing 3 of" in body.lower()


def test_perform_search_money_tool_present_but_gated():
    c = FakeController(services={"hyperliquid"})
    res = asyncio.run(perform_tool_search(c, "hyperliquid", 8))
    assert "gated:money" in res.extracted_content


def test_perform_search_no_match_is_structured():
    c = FakeController()
    res = asyncio.run(perform_tool_search(c, "zzzznotathing", 8))
    assert "no matching tools" in res.extracted_content.lower()
    assert not res.error


# --- perform_tool_describe ---------------------------------------------------

def test_describe_builtin_money_tool_states_gate():
    c = FakeController(services={"x402_invoice"})
    res = asyncio.run(perform_tool_describe(c, "x402_invoice"))
    body = res.extracted_content.lower()
    assert "money" in body and "grant" in body
    assert "capabilities:" in body


def test_describe_mcp_tool_shows_schema_and_invoke():
    mcp = _mcp(("anysite", "anysite_search", "search", "web search",
                {"type": "object", "properties": {"q": {"type": "string"}}}))
    c = FakeController(mcp=mcp)
    res = asyncio.run(perform_tool_describe(c, "anysite_search"))
    body = res.extracted_content
    assert "mcp_execute_tool" in body and '"q"' in body and "anysite" in body


def test_describe_loaded_builtin_lists_actions():
    import types

    class _PM:
        @staticmethod
        def model_json_schema():
            return {"type": "object", "properties": {"path": {"type": "string"}}}

    reg = types.SimpleNamespace(
        get_actions_by_service=lambda s: (
            {"read_file": types.SimpleNamespace(param_model=_PM)} if s == "filesystem" else {}))
    c = FakeController(services={"filesystem"}, loaded=("filesystem",), registry=reg)
    res = asyncio.run(perform_tool_describe(c, "filesystem"))
    body = res.extracted_content
    assert "read_file(path)" in body and "loaded" in body


def test_describe_unknown_is_structured():
    c = FakeController()
    res = asyncio.run(perform_tool_describe(c, "frobnicator"))
    assert "unknown" in res.extracted_content.lower() and not res.error


def test_describe_case_folds_and_aliases():
    c = FakeController(services={"browser_manager"})
    res = asyncio.run(perform_tool_describe(c, "BROWSER"))
    assert "browser" in res.extracted_content


def test_describe_mcp_tool_matches_case_insensitively():
    # MCP tool names are external/uncontrolled; describe must find one regardless of
    # the case the agent copies back from tool_search.
    mcp = _mcp(("Srv", "Srv_DoThing", "DoThing", "does a thing", {"type": "object"}))
    c = FakeController(mcp=mcp)
    res = asyncio.run(perform_tool_describe(c, "srv_dothing"))
    assert "MCP tool" in res.extracted_content and "Srv" in res.extracted_content


def test_describe_does_not_mutate_mcp_schema():
    live = {"type": "object", "properties": {"q": {"type": "string"}}}
    mcp = _mcp(("anysite", "anysite_search", "search", "web search", live))
    c = FakeController(mcp=mcp)
    asyncio.run(perform_tool_describe(c, "anysite_search"))
    assert live == {"type": "object", "properties": {"q": {"type": "string"}}}
