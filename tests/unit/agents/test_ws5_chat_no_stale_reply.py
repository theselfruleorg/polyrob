"""2026-10-13 review C7: after a provider error mid-turn, chat_once (and /v1)
must not return the PREVIOUS turn's answer as the answer to the new question."""
from types import SimpleNamespace

from agents.task_agent_lite import TaskAgent
from modules.llm.messages import AIMessage, HumanMessage


def _setup():
    old_answer = AIMessage(content="Your balance is 3.2 ETH.")
    managed = [SimpleNamespace(message=HumanMessage(content="balance?")),
               SimpleNamespace(message=old_answer)]
    ledger = [object()]
    hist = SimpleNamespace(history=ledger, is_done=lambda: True,
                           final_result=lambda: "Your balance is 3.2 ETH.")
    agent = SimpleNamespace(history=hist, state=SimpleNamespace(n_steps=1),
                            message_manager=SimpleNamespace(
                                history=SimpleNamespace(messages=managed)))
    orch = SimpleNamespace(agents={"main": agent})
    ta = TaskAgent.__new__(TaskAgent)
    ta._registry = SimpleNamespace(get=lambda sid: orch)
    return ta, managed, ledger


def test_a_failed_turn_does_not_return_the_previous_answer():
    ta, managed, _ledger = _setup()
    baseline = ta._chat_turn_baseline("s1")
    # The new turn: the question lands, then the provider fails — no reply.
    managed.append(SimpleNamespace(message=HumanMessage(content="and on Base?")))
    assert ta._extract_chat_reply("s1", since=baseline) == ""


def test_a_real_new_reply_is_returned():
    ta, managed, ledger = _setup()
    baseline = ta._chat_turn_baseline("s1")
    managed.append(SimpleNamespace(message=HumanMessage(content="and on Base?")))
    managed.append(SimpleNamespace(message=AIMessage(content="On Base you hold 0.4 ETH.")))
    assert ta._extract_chat_reply("s1", since=baseline) == "On Base you hold 0.4 ETH."


def test_chat_once_on_a_reused_session_returns_the_failure_text():
    import asyncio
    from unittest.mock import MagicMock
    ta, managed, _ledger = _setup()
    ta._initialized = True
    ta.task_available = True
    ta._chat_sessions = {"chat:u1:c1": "s1"}
    ta.session_manager = MagicMock()
    ta.session_manager.get_session_info.return_value = {"id": "s1"}
    orch = ta._registry.get("s1")

    async def _submit(**kw):
        managed.append(SimpleNamespace(message=HumanMessage(content=kw["text"])))
    orch.submit_user_message = _submit

    async def _run(user_id, session_id):
        return "Session failed: provider 503"
    ta.run_session = _run
    ta._resolve_chat_persona = lambda: _none()
    ta._apply_temperature = lambda *a, **k: None

    async def _none():
        return None
    out = asyncio.run(ta.chat_once("u1", "and on Base?", chat_id="c1"))
    assert out == "Session failed: provider 503"


def test_the_chat_key_survives_a_restart():
    """2026-10-13 review C8: the key -> session map is found again from the
    session metadata after the in-memory map is gone."""
    from unittest.mock import MagicMock
    ta = TaskAgent.__new__(TaskAgent)
    ta._chat_sessions = {}
    ta.session_manager = MagicMock()
    ta.session_manager._sessions = {
        "old": {"id": "old", "user_id": "u1", "chat_session_key": "chat:u1:c1",
                "created_at": "2026-10-12T08:00:00"},
        "new": {"id": "new", "user_id": "u1", "chat_session_key": "chat:u1:c1",
                "created_at": "2026-10-13T08:00:00"},
        "other": {"id": "other", "user_id": "u2", "chat_session_key": "chat:u1:c1",
                  "created_at": "2026-10-14T08:00:00"},
    }
    assert ta._durable_chat_session("chat:u1:c1", "u1") == "new"
    assert ta._chat_sessions["chat:u1:c1"] == "new"
    assert ta._durable_chat_session("chat:u1:zz", "u1") is None
