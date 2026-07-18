"""S2 (chat consolidation) — TaskAgent.chat_once synchronous chat adapter.

chat_once runs ONE synchronous turn of the unified task agent and returns the
ACTUAL assistant reply text — not run_session's generic "Session completed
successfully" and not process_user_message's fire-and-forget ack. It reuses a
durable session keyed by (user_id, chat_id) so follow-ups continue the same
conversation, and runs tool-light.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.task_agent_lite import TaskAgent, SessionRequest
from modules.llm.messages import AIMessage, HumanMessage


def _managed(msg):
    m = MagicMock()
    m.message = msg
    return m


def _agent_with_reply(reply_text, *, is_done_text=None):
    """Build a fake agent whose message_manager history ends with an AIMessage."""
    agent = MagicMock()
    msgs = [_managed(HumanMessage(content="hi")), _managed(AIMessage(content=reply_text))]
    agent.message_manager.history.messages = msgs
    if is_done_text is not None:
        agent.history.final_result.return_value = is_done_text
    else:
        agent.history.final_result.return_value = None
    return agent


def _bare_taskagent():
    ta = TaskAgent.__new__(TaskAgent)
    ta._initialized = True
    ta.task_available = True
    ta._chat_sessions = {}
    ta.session_manager = MagicMock()
    ta._registry = MagicMock()
    return ta


def test_chat_once_returns_assistant_reply_not_ack():
    ta = _bare_taskagent()
    orch = MagicMock()
    orch.agents = {"a1": _agent_with_reply("Hello, I'm Rob!")}
    ta._registry.get.return_value = orch

    created = {}

    async def fake_create_session(user_id, request, **kw):
        created["request"] = request
        created["kwargs"] = kw
        return {"id": "sess-1"}

    async def fake_run_session(user_id, session_id):
        return "Session completed successfully"  # the generic string we must NOT return

    ta.create_session = fake_create_session
    ta.run_session = fake_run_session
    ta.session_manager.get_session_info.return_value = {"id": "sess-1", "user_id": "u1"}

    reply = asyncio.run(ta.chat_once("u1", "hi", chat_id="c1"))
    assert reply == "Hello, I'm Rob!"
    assert reply != "Session completed successfully"


def test_chat_once_skips_credit_check_for_parity():
    # ChatAgent did no credit pre-check; chat_once preserves that parity by
    # passing skip_credit_check=True (CHAT_SKIP_CREDIT_CHECK default ON).
    ta = _bare_taskagent()
    orch = MagicMock()
    orch.agents = {"a1": _agent_with_reply("hi")}
    ta._registry.get.return_value = orch
    captured = {}

    async def fake_create_session(user_id, request, **kw):
        captured["kwargs"] = kw
        return {"id": "sess-1"}

    async def fake_run_session(user_id, session_id):
        return ""

    ta.create_session = fake_create_session
    ta.run_session = fake_run_session
    ta.session_manager.get_session_info.return_value = {"id": "sess-1"}
    asyncio.run(ta.chat_once("u1", "hi", chat_id="c1"))
    assert captured["kwargs"].get("skip_credit_check") is True


def test_chat_once_is_tool_light():
    ta = _bare_taskagent()
    orch = MagicMock()
    orch.agents = {"a1": _agent_with_reply("hi")}
    ta._registry.get.return_value = orch
    captured = {}

    async def fake_create_session(user_id, request, **kw):
        captured["request"] = request
        return {"id": "sess-1"}

    async def fake_run_session(user_id, session_id):
        return ""

    ta.create_session = fake_create_session
    ta.run_session = fake_run_session
    ta.session_manager.get_session_info.return_value = {"id": "sess-1"}

    asyncio.run(ta.chat_once("u1", "hi", chat_id="c1"))
    req = captured["request"]
    # tool-light: a non-empty MINIMAL toolset (empty list would trip the
    # orchestrator's comprehensive-default fallback) and no browser.
    assert req.tools and "browser" not in req.tools
    assert req.use_vision is False


def test_chat_once_reuses_session_by_chat_key():
    ta = _bare_taskagent()
    orch = MagicMock()
    orch.agents = {"a1": _agent_with_reply("again")}
    orch.submit_user_message = AsyncMock()
    ta._registry.get.return_value = orch
    calls = {"create": 0}

    async def fake_create_session(user_id, request, **kw):
        calls["create"] += 1
        return {"id": "sess-1"}

    async def fake_run_session(user_id, session_id):
        return ""

    ta.create_session = fake_create_session
    ta.run_session = fake_run_session
    ta.session_manager.get_session_info.return_value = {"id": "sess-1"}

    asyncio.run(ta.chat_once("u1", "first", chat_id="c1"))
    asyncio.run(ta.chat_once("u1", "second", chat_id="c1"))
    assert calls["create"] == 1  # second call reused the session
    # second call queued a continuation rather than creating
    assert orch.submit_user_message.await_count >= 1


def test_chat_once_different_chat_id_is_separate_session():
    ta = _bare_taskagent()
    orch = MagicMock()
    orch.agents = {"a1": _agent_with_reply("x")}
    ta._registry.get.return_value = orch
    ids = iter(["sess-1", "sess-2"])

    async def fake_create_session(user_id, request, **kw):
        return {"id": next(ids)}

    async def fake_run_session(user_id, session_id):
        return ""

    ta.create_session = fake_create_session
    ta.run_session = fake_run_session
    ta.session_manager.get_session_info.return_value = {"id": "x"}

    asyncio.run(ta.chat_once("u1", "a", chat_id="c1"))
    asyncio.run(ta.chat_once("u1", "b", chat_id="c2"))
    assert ta._chat_sessions["chat:u1:c1"] == "sess-1"
    assert ta._chat_sessions["chat:u1:c2"] == "sess-2"


def test_chat_once_honors_chat_provider_model_env(monkeypatch):
    monkeypatch.setenv("CHAT_PROVIDER", "openrouter")
    monkeypatch.setenv("CHAT_MODEL", "z-ai/glm-5.2")
    ta = _bare_taskagent()
    orch = MagicMock()
    orch.agents = {"a1": _agent_with_reply("hi")}
    ta._registry.get.return_value = orch
    captured = {}

    async def fake_create_session(user_id, request, **kw):
        captured["request"] = request
        return {"id": "sess-1"}

    async def fake_run_session(user_id, session_id):
        return ""

    ta.create_session = fake_create_session
    ta.run_session = fake_run_session
    ta.session_manager.get_session_info.return_value = {"id": "sess-1"}

    asyncio.run(ta.chat_once("u1", "hi", chat_id="c1"))
    assert captured["request"].provider == "openrouter"
    assert captured["request"].model == "z-ai/glm-5.2"


def test_chat_once_default_model_unchanged(monkeypatch):
    # "Defaults preserved" precondition: nothing pinned AND no provider keys present
    # (Phase 2: chat now resolves the first keyed provider when OpenAI is absent, so
    # clear all keys to deterministically exercise the openai/gpt-5 floor).
    for _v in (
        "CHAT_PROVIDER", "CHAT_MODEL", "DEFAULT_PROVIDER", "DEFAULT_MODEL",
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY",
    ):
        monkeypatch.delenv(_v, raising=False)
    ta = _bare_taskagent()
    orch = MagicMock()
    orch.agents = {"a1": _agent_with_reply("hi")}
    ta._registry.get.return_value = orch
    captured = {}

    async def fake_create_session(user_id, request, **kw):
        captured["request"] = request
        return {"id": "sess-1"}

    async def fake_run_session(user_id, session_id):
        return ""

    ta.create_session = fake_create_session
    ta.run_session = fake_run_session
    ta.session_manager.get_session_info.return_value = {"id": "sess-1"}
    asyncio.run(ta.chat_once("u1", "hi", chat_id="c1"))
    # No CHAT_PROVIDER/CHAT_MODEL set => SessionRequest defaults preserved.
    assert captured["request"].provider == "openai"
    assert captured["request"].model == "gpt-5"


def test_extract_reply_skips_brain_json_leak():
    # Live-caught (GLM): the LAST AIMessage can be the model's raw brain-state JSON
    # (added atomically AFTER done()'s clean message). Capture must NOT return it;
    # it returns the clean done() final_result instead.
    ta = _bare_taskagent()
    brain = ('{"page_summary":"","evaluation_previous_goal":"Success","memory":"user asked X",'
             '"next_goal":"answer","reasoning":"testing memory"}')
    agent = MagicMock()
    agent.message_manager.history.messages = [
        _managed(AIMessage(content="✅ Task Complete\n\nHere is your clean answer.")),
        _managed(AIMessage(content=brain)),  # raw brain-JSON is LAST
    ]
    agent.history.is_done.return_value = True
    agent.history.final_result.return_value = "Here is your clean answer."
    orch = MagicMock()
    orch.agents = {"a1": agent}
    ta._registry.get.return_value = orch
    reply = ta._extract_chat_reply("sess-1")
    assert reply == "Here is your clean answer."
    assert "page_summary" not in reply


def test_extract_reply_conversational_skips_brain_and_takes_clean_aimessage():
    # No done(): is_done False. Last AIMessage is brain-JSON; the one before is the
    # real send_message text. Capture skips brain and returns the clean reply.
    ta = _bare_taskagent()
    brain = '{"next_goal":"x","memory":"y","reasoning":"z"}'
    agent = MagicMock()
    agent.message_manager.history.messages = [
        _managed(AIMessage(content="Hey there, happy to help!")),
        _managed(AIMessage(content=brain)),
    ]
    agent.history.is_done.return_value = False
    agent.history.final_result.return_value = None
    orch = MagicMock()
    orch.agents = {"a1": agent}
    ta._registry.get.return_value = orch
    reply = ta._extract_chat_reply("sess-1")
    assert reply == "Hey there, happy to help!"


def test_chat_once_done_path_uses_final_result():
    # When the turn ends with done() (no trailing AIMessage send), capture
    # falls back to history.final_result().
    ta = _bare_taskagent()
    agent = MagicMock()
    agent.message_manager.history.messages = []  # no AIMessage
    agent.history.final_result.return_value = "the done answer"
    orch = MagicMock()
    orch.agents = {"a1": agent}
    ta._registry.get.return_value = orch

    async def fake_create_session(user_id, request, **kw):
        return {"id": "sess-1"}

    async def fake_run_session(user_id, session_id):
        return "Session completed successfully"

    ta.create_session = fake_create_session
    ta.run_session = fake_run_session
    ta.session_manager.get_session_info.return_value = {"id": "sess-1"}

    reply = asyncio.run(ta.chat_once("u1", "hi", chat_id="c1"))
    assert reply == "the done answer"


def test_chat_once_concurrent_same_key_serializes_no_duplicate_session():
    """Two concurrent turns for the SAME (user_id, chat_id) must not both create a
    session — the per-chat-key lock serializes them so the second reuses the first."""
    ta = _bare_taskagent()
    orch = MagicMock()
    orch.agents = {"a1": _agent_with_reply("ok")}
    orch.submit_user_message = AsyncMock()
    ta._registry.get.return_value = orch
    calls = {"create": 0}

    async def fake_create_session(user_id, request, **kw):
        calls["create"] += 1
        await asyncio.sleep(0.02)  # widen the create window to expose a race
        return {"id": "sess-1"}

    # Session only "exists" after the first create completes.
    def get_info(sid):
        return {"id": "sess-1"} if calls["create"] else None

    async def fake_run_session(user_id, session_id):
        await asyncio.sleep(0.01)
        return ""

    ta.create_session = fake_create_session
    ta.run_session = fake_run_session
    ta.session_manager.get_session_info.side_effect = get_info

    async def drive():
        await asyncio.gather(
            ta.chat_once("u1", "first", chat_id="c1"),
            ta.chat_once("u1", "second", chat_id="c1"),
        )

    asyncio.run(drive())
    assert calls["create"] == 1  # serialized: exactly one session created


# --- B3: per-request model routing -----------------------------------------
# chat_once(provider=, model=) threads a per-request model override into a
# reused session's LIVE agent via swap_model (idempotence-guarded so an
# unchanged model doesn't rebuild the LLM every request). A brand-new session
# gets the override baked into its SessionRequest instead (no live agent
# exists yet to swap).


def _reused_session_taskagent(agent):
    """A _bare_taskagent() pre-wired with an existing (user_id, chat_id) session
    whose orchestrator already holds `agent`, so chat_once takes the REUSE branch."""
    ta = _bare_taskagent()
    orch = MagicMock()
    orch.agents = {"a1": agent}
    orch.submit_user_message = AsyncMock()
    ta._registry.get.return_value = orch
    ta._chat_sessions["chat:u1:c1"] = "sess-1"
    ta.session_manager.get_session_info.return_value = {"id": "sess-1"}

    async def fake_run_session(user_id, session_id):
        return ""

    ta.run_session = fake_run_session
    return ta, orch


def test_chat_once_swaps_model_for_reused_session():
    agent = _agent_with_reply("hi")
    agent.model_name = "gpt-5"
    agent.llm_provider = "openai"
    agent.swap_model = AsyncMock(return_value={"ok": True})
    ta, _ = _reused_session_taskagent(agent)

    asyncio.run(ta.chat_once(
        "u1", "hi", chat_id="c1", provider="anthropic", model="claude-sonnet-4-5",
    ))
    agent.swap_model.assert_awaited_once_with("anthropic", "claude-sonnet-4-5")


def test_chat_once_swap_happens_before_the_turn_runs():
    order = []
    agent = _agent_with_reply("hi")
    agent.model_name = "gpt-5"
    agent.llm_provider = "openai"

    async def fake_swap(provider, model):
        order.append("swap")
        return {"ok": True}

    agent.swap_model = fake_swap
    ta, _ = _reused_session_taskagent(agent)

    async def fake_run_session(user_id, session_id):
        order.append("run")
        return ""

    ta.run_session = fake_run_session

    asyncio.run(ta.chat_once(
        "u1", "hi", chat_id="c1", provider="anthropic", model="claude-sonnet-4-5",
    ))
    assert order == ["swap", "run"]


def test_chat_once_no_model_kwarg_skips_swap():
    agent = _agent_with_reply("hi")
    agent.model_name = "gpt-5"
    agent.llm_provider = "openai"
    agent.swap_model = AsyncMock(return_value={"ok": True})
    ta, _ = _reused_session_taskagent(agent)

    asyncio.run(ta.chat_once("u1", "hi", chat_id="c1"))
    agent.swap_model.assert_not_awaited()


def test_chat_once_same_provider_and_model_skips_swap_idempotence():
    agent = _agent_with_reply("hi")
    agent.model_name = "claude-sonnet-4-5"
    agent.llm_provider = "anthropic"
    agent.swap_model = AsyncMock(return_value={"ok": True})
    ta, _ = _reused_session_taskagent(agent)

    asyncio.run(ta.chat_once(
        "u1", "hi", chat_id="c1", provider="anthropic", model="claude-sonnet-4-5",
    ))
    agent.swap_model.assert_not_awaited()


def test_chat_once_falsy_provider_still_swaps_when_model_differs():
    # provider falsy => swap_model auto-detects; the guard must not treat a
    # falsy provider as "same provider" and skip a genuine model change.
    agent = _agent_with_reply("hi")
    agent.model_name = "gpt-5"
    agent.llm_provider = "openai"
    agent.swap_model = AsyncMock(return_value={"ok": True})
    ta, _ = _reused_session_taskagent(agent)

    asyncio.run(ta.chat_once(
        "u1", "hi", chat_id="c1", provider=None, model="claude-sonnet-4-5",
    ))
    agent.swap_model.assert_awaited_once_with(None, "claude-sonnet-4-5")


def test_chat_once_swap_failure_logs_and_continues():
    agent = _agent_with_reply("hi")
    agent.model_name = "gpt-5"
    agent.llm_provider = "openai"
    agent.swap_model = AsyncMock(return_value={"ok": False, "error": "boom"})
    ta, _ = _reused_session_taskagent(agent)

    reply = asyncio.run(ta.chat_once(
        "u1", "hi", chat_id="c1", provider="anthropic", model="claude-sonnet-4-5",
    ))
    agent.swap_model.assert_awaited_once_with("anthropic", "claude-sonnet-4-5")
    assert reply == "hi"  # turn still completes on the (unchanged) current model


def test_chat_once_never_swapped_agent_idempotent_via_provider_name():
    # Critical 2: a never-swapped agent has no `.llm_provider` set to the request
    # provider (it may be None); the guard must fall back to the MessageManager
    # `provider_name` SSOT so an unchanged model does NOT rebuild the LLM.
    agent = _agent_with_reply("hi")
    agent.model_name = "claude-sonnet-4-5"
    agent.llm_provider = None                # never swapped
    agent.provider_name = "anthropic"        # MessageManager SSOT
    agent.swap_model = AsyncMock(return_value={"ok": True})
    ta, _ = _reused_session_taskagent(agent)

    asyncio.run(ta.chat_once(
        "u1", "hi", chat_id="c1", provider="anthropic", model="claude-sonnet-4-5",
    ))
    agent.swap_model.assert_not_awaited()


def test_chat_once_swap_raising_does_not_500_and_turn_proceeds():
    # Critical 2: an exception from swap_model must be caught (warn + proceed on
    # the current model), never propagate up and 500 the /v1 request.
    agent = _agent_with_reply("hi")
    agent.model_name = "gpt-5"
    agent.llm_provider = "openai"
    agent.swap_model = AsyncMock(side_effect=RuntimeError("provider build blew up"))
    ta, _ = _reused_session_taskagent(agent)

    reply = asyncio.run(ta.chat_once(
        "u1", "hi", chat_id="c1", provider="anthropic", model="claude-sonnet-4-5",
    ))
    agent.swap_model.assert_awaited_once_with("anthropic", "claude-sonnet-4-5")
    assert reply == "hi"  # turn still completes on the unchanged current model


def test_chat_once_new_session_bakes_requested_model_into_session_request():
    # No live agent exists yet for a brand-new session, so the override goes
    # into the SessionRequest instead of a swap_model call.
    ta = _bare_taskagent()
    captured = {}

    async def fake_create_session(user_id, request, **kw):
        captured["request"] = request
        return {"id": "sess-1"}

    async def fake_run_session(user_id, session_id):
        return ""

    ta.create_session = fake_create_session
    ta.run_session = fake_run_session
    ta.session_manager.get_session_info.return_value = {"id": "sess-1"}
    orch = MagicMock()
    orch.agents = {}
    ta._registry.get.return_value = orch

    asyncio.run(ta.chat_once(
        "u1", "hi", chat_id="c1", provider="anthropic", model="claude-sonnet-4-5",
    ))
    assert captured["request"].provider == "anthropic"
    assert captured["request"].model == "claude-sonnet-4-5"
