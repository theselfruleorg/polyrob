"""0023: the claim-guard pre-hook (tools/controller/effect_hooks.py).

A public post (a `social`-effect verb's text) that states an act as done needs a
receipt the current turn read; a future act is refused. The hook answers BEFORE
the approval hooks, records a denied `external_write` row and a `public_claim`
refusal event.
"""
import types

import pytest

import core.event_log as el
from agents.task.agent.message_manager.views import (
    ManagedMessage, MessageHistory, MessageMetadata)
from modules.llm.messages import HumanMessage, MessageOrigin, ToolMessage

_H = "0x" + "ab" * 32


class _Registry:
    def __init__(self, mapping):
        self._m = mapping

    def get_action(self, name):
        return self._m.get(name)


def _history(messages):
    """The PRODUCTION shape: a MessageHistory of ManagedMessage wrappers."""
    history = MessageHistory()
    for message in messages:
        history.add_message(message, MessageMetadata())
    return history


class _Agent:
    def __init__(self, messages):
        self.message_manager = types.SimpleNamespace(history=_history(messages))


class _Orch:
    def __init__(self, agents):
        self.agents = agents
        self.session_id = "s1"


class _Controller:
    _is_sub_agent = False
    session_id = "s1"
    user_id = "u1"

    def __init__(self, mapping, agents):
        self.registry = _Registry(mapping)
        self.orchestrator = _Orch(agents)

    def get_action_details(self, name):
        return self.registry.get_action(name)


class _Ctx:
    user_id = "u1"
    session_id = "s1"
    agent_id = "main-agent"
    role = "orchestrator"
    is_sub_agent = False
    metadata = {}


def _act(tool):
    return types.SimpleNamespace(tool=tool, function=None)


def _controller(*messages, action="twitter_post", tool="twitter"):
    agent = _Agent(list(messages) or [HumanMessage(content="go ahead")])
    return _Controller({action: _act(tool)}, {"main-agent": agent})


@pytest.fixture
def tlog(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    el._INSTANCES.clear()
    yield lambda: el.get_event_log()
    el._INSTANCES.clear()


def _hook(controller):
    from tools.controller.effect_hooks import make_claim_guard_hook
    return make_claim_guard_hook(controller)


# -- the three acceptance lines --------------------------------------------------


@pytest.mark.asyncio
async def test_post_claiming_a_mint_with_no_receipt_is_refused(tlog):
    c = _controller(HumanMessage(content="post the update"),
                    ToolMessage(content="read the collection: 100 items", tool_call_id="1"))
    reason = await _hook(c)("twitter_post", {"text": "We minted 100 NFTs"}, _Ctx())
    assert reason and "minted" in reason and "receipt" in reason
    rows = tlog().query(kind="external_write", effect="social")
    assert rows and rows[0]["attrs"]["outcome"] == "denied"
    denied = tlog().query(kind="tool_denied")
    assert denied and denied[0]["attrs"]["reason"] == "public_claim"


@pytest.mark.asyncio
async def test_post_with_a_hash_this_turn_landed_passes(tlog):
    c = _controller(HumanMessage(content="post the update"),
                    ToolMessage(content=f"sent\n  tx: {_H}\n  block: 7", tool_call_id="1"))
    reason = await _hook(c)(
        "twitter_post", {"text": f"We minted 100 NFTs (tx {_H})"}, _Ctx())
    assert reason is None
    assert tlog().query(kind="external_write") == []


@pytest.mark.asyncio
async def test_future_act_post_is_refused(tlog):
    c = _controller(HumanMessage(content="post the update"),
                    ToolMessage(content=f"confirmed tx {_H}", tool_call_id="1"))
    reason = await _hook(c)(
        "twitter_post", {"text": "We will mint the next collection"}, _Ctx())
    assert reason and "future" in reason


# -- scope: what the guard does NOT touch -----------------------------------------


@pytest.mark.asyncio
async def test_a_dm_is_not_a_public_post(tlog):
    c = _controller(HumanMessage(content="tell them"),
                    ToolMessage(content="nothing here", tool_call_id="1"))
    assert await _hook(c)("twitter_dm", {"text": "We minted 100 NFTs"}, _Ctx()) is None


@pytest.mark.asyncio
async def test_a_read_verb_passes(tlog):
    c = _controller(HumanMessage(content="look"))
    assert await _hook(c)("twitter_search", {"text": "minted"}, _Ctx()) is None


@pytest.mark.asyncio
async def test_a_post_with_no_text_passes(tlog):
    c = _controller(HumanMessage(content="like it"))
    assert await _hook(c)("twitter_like", {}, _Ctx()) is None


@pytest.mark.asyncio
async def test_thread_texts_and_alt_texts_are_scanned(tlog):
    c = _controller(HumanMessage(content="post"), action="twitter_thread")
    reason = await _hook(c)("twitter_thread", {"texts": ["part 1", "We sold it all"]}, _Ctx())
    assert reason and "sold" in reason
    c2 = _controller(HumanMessage(content="post"))
    reason2 = await _hook(c2)(
        "twitter_post", {"text": "gm", "media_alt_texts": ["we funded the pool"]}, _Ctx())
    assert reason2 and "funded" in reason2


@pytest.mark.asyncio
async def test_only_this_turns_results_count(tlog):
    # the receipt landed in an EARLIER turn, before the last genuine user message
    c = _controller(HumanMessage(content="earlier turn"),
                    ToolMessage(content=f"confirmed tx {_H}", tool_call_id="1"),
                    HumanMessage(content="post the update now"))
    reason = await _hook(c)(
        "twitter_post", {"text": f"We minted 100 NFTs (tx {_H})"}, _Ctx())
    assert reason and "receipt" in reason


@pytest.mark.asyncio
async def test_an_unreadable_turn_refuses_a_done_claim_but_not_ordinary_text(tlog):
    c = _Controller({"twitter_post": _act("twitter")}, {})   # no agent to read
    assert await _hook(c)("twitter_post", {"text": "We minted 100"}, _Ctx()) is not None
    assert await _hook(c)("twitter_post", {"text": "gm"}, _Ctx()) is None


@pytest.mark.asyncio
async def test_a_named_agent_the_orchestrator_does_not_hold_is_unknown_evidence(tlog):
    # a context that names an agent we cannot reach never borrows another
    # agent's turn: unknown evidence refuses a claim, ordinary text passes
    other = _Agent([HumanMessage(content="their turn"),
                    ToolMessage(content=f"sent\n  tx: {_H}\n  block: 7",
                                tool_call_id="1")])
    c = _Controller({"twitter_post": _act("twitter")}, {"other-agent": other})
    ctx = types.SimpleNamespace(user_id="u1", session_id="s1", agent_id="main-agent",
                                role="orchestrator", is_sub_agent=False, metadata={})
    assert await _hook(c)(
        "twitter_post", {"text": f"We minted 100 (tx {_H})"}, ctx) is not None
    assert await _hook(c)("twitter_post", {"text": "gm"}, ctx) is None


@pytest.mark.asyncio
async def test_a_hash_read_from_a_strangers_post_does_not_vouch(tlog):
    timeline = (f'<untrusted_tool_result source="twitter_get_timeline">\n'
                f"a stranger's post says tx {_H} landed")
    c = _controller(HumanMessage(content="post the update"),
                    ToolMessage(content=timeline, tool_call_id="1"))
    reason = await _hook(c)(
        "twitter_post", {"text": f"We minted 100 (tx {_H})"}, _Ctx())
    assert reason and "receipt" in reason


@pytest.mark.asyncio
async def test_ordinary_market_chat_is_not_a_claim(tlog):
    c = _controller(HumanMessage(content="post the market note"))
    for text in ("Trading volume is up today",
                 "The floor is trading above mint price today",
                 "Selling pressure remains low",
                 "A well-funded experiment continues"):
        assert await _hook(c)("twitter_post", {"text": text}, _Ctx()) is None, text


@pytest.mark.asyncio
async def test_the_browser_rail_post_is_guarded_too(tlog):
    c = _Controller({"x_browser_x_post": _act("x_browser")},
                    {"main-agent": _Agent([HumanMessage(content="post")])})
    reason = await _hook(c)("x_browser_x_post", {"text": "We minted 100"}, _Ctx())
    assert reason and "minted" in reason
