"""Dynamic tool rig (progressive tool disclosure, S1+S2 — 2026-07-19 handoff).

S1: resolve_tool_status + render_tool_catalog — honest per-tool status
    (loaded / loadable / gated:<reason>) from the existing SSOTs
    (tools/descriptors.py + core/tool_capabilities.py + the container).
S2: perform_load_tool — the thin-closure helper behind the load_tool action
    (mirrors tools/controller/message_send.perform_message_send).

Hard lines under test: money tools are NEVER loadable via load_tool; a
delegated leaf never loads a delegate-blocked id; refusals are STRUCTURED
(reason + remedy channel), not silent drops.
"""
import asyncio

from tools.tool_disclosure import (
    perform_load_tool,
    render_tool_catalog,
    resolve_tool_status,
)


class FakeContainer:
    def __init__(self, services=()):
        self._services = {s: object() for s in services}

    def has_service(self, name):
        return name in self._services

    def get_service(self, name):
        return self._services.get(name)


# --- resolve_tool_status -----------------------------------------------------

def test_loaded_when_in_session():
    st = resolve_tool_status(
        "web_fetch", container=FakeContainer({"web_fetch"}), loaded_ids={"web_fetch"})
    assert st.status == "loaded"


def test_loadable_when_container_has_service():
    st = resolve_tool_status(
        "web_fetch", container=FakeContainer({"web_fetch"}), loaded_ids=set())
    assert st.status == "loadable"
    assert "load_tool" in st.remedy


def test_loadable_via_tool_suffix_service():
    st = resolve_tool_status(
        "email", container=FakeContainer({"email_tool"}), loaded_ids=set())
    assert st.status == "loadable"


def test_browser_loadable_via_browser_manager():
    st = resolve_tool_status(
        "browser", container=FakeContainer({"browser_manager"}), loaded_ids=set())
    assert st.status == "loadable"


def test_money_tool_never_loadable():
    # Even when the container could serve it, a money tool is explicit-grant-only.
    st = resolve_tool_status(
        "hyperliquid", container=FakeContainer({"hyperliquid"}), loaded_ids=set())
    assert st.status == "gated"
    assert st.reason == "money"
    assert "owner" in st.remedy


def test_money_tool_shows_loaded_when_explicitly_granted():
    # An operator/goal-payload grant at session creation is honest 'loaded'.
    st = resolve_tool_status(
        "hyperliquid", container=FakeContainer({"hyperliquid"}),
        loaded_ids={"hyperliquid"})
    assert st.status == "loaded"


def test_leaf_blocked_for_delegate_blocked_tool():
    st = resolve_tool_status(
        "cronjob", container=FakeContainer({"cronjob"}), loaded_ids=set(),
        is_leaf=True)
    assert st.status == "gated"
    assert st.reason == "leaf-blocked"


def test_leaf_can_load_unblocked_tool():
    st = resolve_tool_status(
        "web_fetch", container=FakeContainer({"web_fetch"}), loaded_ids=set(),
        is_leaf=True)
    assert st.status == "loadable"


def test_unavailable_with_config_remedy():
    st = resolve_tool_status(
        "perplexity", container=FakeContainer(), loaded_ids=set())
    assert st.status == "gated"
    assert st.reason == "unavailable-on-this-deploy"
    assert "perplexity_api_key" in st.remedy


def test_unavailable_without_config_names_deploy_shape():
    st = resolve_tool_status("mcp", container=FakeContainer(), loaded_ids=set())
    assert st.status == "gated"
    assert st.reason == "unavailable-on-this-deploy"
    assert st.remedy  # never an empty remedy — the agent must know the channel


def test_unknown_tool_id_is_structured():
    st = resolve_tool_status(
        "frobnicator", container=FakeContainer({"frobnicator"}), loaded_ids=set())
    assert st.status == "gated"
    assert st.reason == "unknown-tool"


def test_alias_is_normalized_to_display_id():
    st = resolve_tool_status(
        "browser_manager", container=FakeContainer({"browser_manager"}),
        loaded_ids=set())
    assert st.tool_id == "browser"
    assert st.status == "loadable"


# --- render_tool_catalog -----------------------------------------------------

def test_catalog_block_shape_and_statuses():
    cat = render_tool_catalog(
        container=FakeContainer({"web_fetch"}), loaded_ids={"filesystem"})
    assert cat.startswith("<tool-catalog>")
    assert cat.rstrip().endswith("</tool-catalog>")
    # Alias dedupe: render the display id 'browser', never 'browser_manager'.
    assert "browser_manager" not in cat
    lines = {l.split(":", 1)[0].strip("- "): l for l in cat.splitlines()
             if l.startswith("- ")}
    assert "[loaded]" in lines["filesystem"]
    assert "loadable" in lines["web_fetch"]
    assert "gated:money" in lines["hyperliquid"]
    # The header teaches the load_tool verb.
    assert "load_tool" in cat


def test_catalog_one_line_per_display_tool():
    cat = render_tool_catalog(container=FakeContainer(), loaded_ids=set())
    ids = [l.split(":", 1)[0].strip("- ") for l in cat.splitlines()
           if l.startswith("- ")]
    assert len(ids) == len(set(ids))
    assert "browser" in ids


# --- perform_load_tool (S2) --------------------------------------------------

class FakeController:
    def __init__(self, services=(), loaded=(), is_sub=False):
        self.container = FakeContainer(services)
        self._loaded = list(loaded)
        self._is_sub_agent = is_sub
        self.load_calls = []

    def list_tools(self):
        return list(self._loaded)

    async def load_tools_from_container(self, ids):
        self.load_calls.append(list(ids))
        out = {}
        for i in ids:
            if (self.container.has_service(i)
                    or self.container.has_service(f"{i}_tool")):
                self._loaded.append(i)
                out[i] = object()
        return out


class FakeExecutionContext:
    def __init__(self, is_sub_agent=False, role="orchestrator"):
        self.is_sub_agent = is_sub_agent
        self.role = role


def test_perform_load_tool_loads_available_tool():
    c = FakeController(services={"web_fetch"})
    res = asyncio.run(perform_load_tool(c, "web_fetch"))
    assert not res.error
    assert c.load_calls == [["web_fetch"]]
    assert "web_fetch" in c.list_tools()
    assert "next step" in (res.extracted_content or "").lower()


def test_perform_load_tool_money_refused_before_any_load():
    c = FakeController(services={"hyperliquid"})
    res = asyncio.run(perform_load_tool(c, "hyperliquid"))
    assert c.load_calls == []
    assert "gated:money" in (res.extracted_content or "")


def test_perform_load_tool_leaf_refused_for_blocked_id():
    c = FakeController(services={"cronjob"})
    ctx = FakeExecutionContext(is_sub_agent=True, role="leaf")
    res = asyncio.run(perform_load_tool(c, "cronjob", execution_context=ctx))
    assert c.load_calls == []
    assert "gated:leaf-blocked" in (res.extracted_content or "")


def test_perform_load_tool_unavailable_returns_remedy():
    c = FakeController()
    res = asyncio.run(perform_load_tool(c, "perplexity"))
    assert c.load_calls == []
    assert "perplexity_api_key" in (res.extracted_content or "")


def test_perform_load_tool_already_loaded_is_noop():
    c = FakeController(services={"web_fetch"}, loaded=("web_fetch",))
    res = asyncio.run(perform_load_tool(c, "web_fetch"))
    assert c.load_calls == []
    assert "already loaded" in (res.extracted_content or "")


def test_perform_load_tool_container_load_failure_is_error():
    # Status says loadable (service present) but the load itself comes back empty.
    class FlakyController(FakeController):
        async def load_tools_from_container(self, ids):
            self.load_calls.append(list(ids))
            return {}

    c = FlakyController(services={"web_fetch"})
    res = asyncio.run(perform_load_tool(c, "web_fetch"))
    assert res.error
