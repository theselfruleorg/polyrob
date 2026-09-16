"""044 I13 — the PRODUCTION `_public_session` stamp, end to end.

Everything that protects a room reads ONE flag: `orchestrator._public_session`
(`core/surfaces/room_policy.py::is_public_session`). Every other 044 test either
sets that flag by hand or inspects source text, so the two ways production
actually builds a room session — `TaskAgent.create_session(session_source=…)`
and `_resolve_or_recreate` after an eviction — were never exercised. Both were
broken:

* C2 — the stamp lived only inside `bind_chat_surface`, which returns EARLY when
  `SINGULAR_CHAT_ENABLED` is off (the default). A room session was built
  private-shaped: owner docs, tenant recall, episodic digest, ungated toolset.
* C1 — the recreate path rebuilt the orchestrator with `chat_type="dm"`
  hardcoded and restored `request.tools` (the DEFAULT browser/filesystem/task
  set), so after ANY eviction or restart the next room line ran with the full
  private toolset and no gate.

These tests build a REAL `TaskAgent` over a REAL `DependencyContainer` and a
REAL `SessionOrchestrator`; only the LLM is a stub. They assert the three things
that MAKE a room session safe: no owner-tenant state in the foundation, the
read-only room toolset, and a fail-closed gate that refuses `goal_create`.
"""
import types

import pytest

from core.surfaces.envelopes import SessionSource
from core.surfaces.room_policy import room_tool_ids
from modules.llm.messages import MessageOrigin

#: Origins a PUBLIC session's foundation may never carry (044 §6 T1).
_FORBIDDEN_ORIGINS = frozenset({
    MessageOrigin.MEMORY, MessageOrigin.RECALL, MessageOrigin.EPISODIC_DIGEST,
    MessageOrigin.PROJECT_CONTEXT, MessageOrigin.ENVIRONMENT,
    MessageOrigin.SESSION_BRIDGE,
})

_OWNER_DOC_MARKER = "OWNER_FACT_ROOM_LEAK_CANARY"
_SOUL_MARKER = "SOUL_ROOM_LEAK_CANARY"
_ENV_MARKER = "ENVIRONMENT_ROOM_LEAK_CANARY"


class _StubLLM:
    """Just enough surface for Agent construction; never called."""
    model_name = "stub-model"
    provider = "openai"

    def get_num_tokens(self, text):  # pragma: no cover - defensive
        return max(1, len(str(text)) // 4)


def _room_source(chat_id="-1001234567890"):
    return SessionSource(surface_id="telegram", chat_id=chat_id,
                         chat_type="supergroup")


def _seed_owner_docs(data_dir):
    """Write the owner-tenant docs a room reply must never be able to quote."""
    from core.instance import resolve_instance_id
    identity = data_dir / "identity" / resolve_instance_id()
    (identity / "user_owner1").mkdir(parents=True, exist_ok=True)
    (identity / "SELF_CONTEXT.md").write_text(f"# Soul\n{_SOUL_MARKER}\n")
    (identity / "user_owner1" / "OWNER.md").write_text(
        f"# Owner facts\n{_OWNER_DOC_MARKER}\n")


@pytest.fixture
def room_env(tmp_path, monkeypatch):
    """A tenant data home with owner docs, and the flags a room run sees."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    # The C2 shape: rooms on, the Singular Chat bus OFF. The stamp must not care.
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.delenv("SINGULAR_CHAT_ENABLED", raising=False)
    monkeypatch.delenv("GROUP_TURN_TOOLS", raising=False)
    monkeypatch.setenv("MEMORY_BACKEND", "none")
    # Force the <environment> block to RENDER, so "no ENVIRONMENT origin" is a
    # real assertion rather than one that passes because a plain server session
    # would not have pinned one anyway (044 I5).
    import agents.task.agent.core.env_context as env_context
    monkeypatch.setattr(env_context, "build_environment_context",
                        lambda *a, **k: f"<environment>\n{_ENV_MARKER}\n</environment>")
    _seed_owner_docs(tmp_path)
    return tmp_path


async def _build_task_agent(tmp_path):
    from agents.task.agent.session import SessionManager
    from agents.task_agent_lite import TaskAgent
    from core.config import BotConfig
    from core.container import DependencyContainer

    config = BotConfig()
    config.data_dir = str(tmp_path)
    container = DependencyContainer.get_instance(config)
    agent = TaskAgent(config=config, container=container)
    agent.session_manager = SessionManager(base_dir=str(tmp_path / "sessions"))
    agent.task_available = True
    agent._initialized = True
    return agent


async def _create_room_session(task_agent, *, user_id="owner1"):
    src = _room_source()
    info = await task_agent.create_session(
        user_id,
        request={"task": "<addressed>\nmember|9 (member) → you: hi\n</addressed>"},
        session_source=src,
        chat_session_key="agent:main:telegram:supergroup:-1001234567890",
        tool_ids=room_tool_ids(),
        skip_credit_check=True,
    )
    return info["id"]


async def _foundation_origins(orchestrator):
    """Build the REAL agent and return (origins, rendered_text)."""
    agent = await orchestrator.create_agent(task="answer the room",
                                            llm=_StubLLM(), agent_name="executor")
    messages = agent.message_manager.get_messages_for_llm(consume_ephemeral=False)
    origins = {getattr(m, "origin", None) for m in messages}
    text = "\n".join(str(getattr(m, "content", "")) for m in messages)
    return agent, origins, text


async def _assert_public_shape(task_agent, session_id, orchestrator):
    """The three invariants that make a room session safe."""
    assert orchestrator._public_session is True, "room session is not PUBLIC"

    # 1. toolset — exactly the read-only room set, resolved the same way
    #    `_start_task_session` resolves it, and NOT widened by the orchestrator's
    #    base defaults (which carry `filesystem`: read AND write on the owner
    #    tenant's session workspace).
    assert sorted(orchestrator._requested_tool_ids) == sorted(room_tool_ids())
    assert "filesystem" not in orchestrator._requested_tool_ids
    assert "browser" not in orchestrator._requested_tool_ids

    # 2. foundation — no owner-tenant state of any origin, and the owner's OWN
    #    documents are not quotable from it.
    agent, origins, text = await _foundation_origins(orchestrator)
    leaked = origins & _FORBIDDEN_ORIGINS
    assert not leaked, f"PUBLIC foundation carried owner-state origins: {leaked}"
    assert _OWNER_DOC_MARKER not in text, "owner facts reached a public room"
    assert _SOUL_MARKER not in text, "the SOUL doc reached a public room"
    assert _ENV_MARKER not in text, "the <environment> block reached a public room"

    # 3. the gate — fail-closed, and it refuses deferred execution by name.
    denial = await orchestrator.controller._run_pre_tool_call_hooks(
        "goal_create", {}, types.SimpleNamespace(user_id="owner1", role="orchestrator"))
    assert denial, "the room gate allowed goal_create"
    assert "group chat" in str(denial).lower()
    return agent


@pytest.mark.asyncio
async def test_create_session_builds_a_public_room_session(room_env):
    """The production create path stamps PUBLIC with the surface bus OFF (C2)."""
    task_agent = await _build_task_agent(room_env)
    session_id = await _create_room_session(task_agent)
    orchestrator = task_agent.get_orchestrator(session_id)
    await _assert_public_shape(task_agent, session_id, orchestrator)


@pytest.mark.asyncio
async def test_recreated_room_session_is_still_public(room_env, monkeypatch):
    """Evict, recreate through the production rail, assert the SAME shape (C1).

    Before the fix this came back with `chat_type="dm"` (so `_public_session`
    was cleared) and `['browser', 'filesystem', 'task']` (so the room had
    filesystem write and a browser)."""
    task_agent = await _build_task_agent(room_env)
    session_id = await _create_room_session(task_agent)

    # Evict: drop the resident orchestrator exactly as the reaper does, leaving
    # only what is on disk.
    task_agent.remove_orchestrator(session_id)
    assert task_agent.get_orchestrator(session_id) is None

    async def _stub_llm_for_request(_request):
        return _StubLLM()

    monkeypatch.setattr(task_agent, "_get_llm_for_request", _stub_llm_for_request)

    session_info = task_agent.session_manager.get_session_info(session_id)
    orchestrator = await task_agent._resolve_or_recreate(session_id, session_info)
    assert orchestrator is not None, "the room session could not be recreated"
    await _assert_public_shape(task_agent, session_id, orchestrator)


@pytest.mark.asyncio
async def test_dm_session_is_not_public(room_env):
    """The control: a DM built by the same path keeps the private profile, so the
    assertions above cannot pass for the wrong reason."""
    task_agent = await _build_task_agent(room_env)
    info = await task_agent.create_session(
        "owner1", request={"task": "hello"},
        session_source=SessionSource(surface_id="telegram", chat_id="42",
                                     chat_type="dm"),
        chat_session_key="agent:main:telegram:dm:42",
        skip_credit_check=True,
    )
    orchestrator = task_agent.get_orchestrator(info["id"])
    assert orchestrator._public_session is False
    agent, origins, text = await _foundation_origins(orchestrator)
    # The control that keeps the public assertions from passing vacuously: in a
    # DM the <environment> block really is pinned.
    assert MessageOrigin.ENVIRONMENT in origins and _ENV_MARKER in text
    denial = await orchestrator.controller._run_pre_tool_call_hooks(
        "goal_create", {}, types.SimpleNamespace(user_id="owner1", role="orchestrator"))
    assert not denial


@pytest.mark.asyncio
async def test_a_pre_044_room_record_recreates_public(room_env, monkeypatch):
    """044 C1 round 2: a session created BEFORE this wave has no
    `public_session`/`effective_tools` keys, and prod holds exactly such a row (a
    July `session_chat_map` binding for a live room). `bool(None)` is False and
    the legacy `request.tools` chain restored browser+filesystem — so the OLDEST,
    longest-lived room sessions, the ones most likely to be recreated, came back
    private-shaped. The binding KEY is the fallback: it IS the address, and a
    room key means a room whatever the record forgot to say."""
    from core.surfaces.session_chat_registry import SessionChatRegistry

    task_agent = await _build_task_agent(room_env)
    session_id = await _create_room_session(task_agent)

    # Age the record back to a pre-044 shape: drop BOTH new keys, and put the
    # private default toolset in `request.tools` the way a July record has it.
    info = task_agent.session_manager.get_session_info(session_id)
    info.pop("public_session", None)
    info.pop("effective_tools", None)
    info["tools"] = ["browser", "filesystem", "task"]
    info.setdefault("request", {})["tools"] = ["browser", "filesystem", "task"]

    # The durable chat<->session row is what remembers this is a room.
    key = "agent:main:telegram:supergroup:-1001234567890"
    registry = SessionChatRegistry(str(room_env / "chat.db"))
    registry.bind(key, session_id, "owner1", "telegram", "-1001234567890")
    task_agent.container.register_service("session_chat_registry", registry)

    task_agent.remove_orchestrator(session_id)

    async def _stub_llm_for_request(_request):
        return _StubLLM()

    monkeypatch.setattr(task_agent, "_get_llm_for_request", _stub_llm_for_request)

    orchestrator = await task_agent._resolve_or_recreate(session_id, info)
    assert orchestrator is not None
    await _assert_public_shape(task_agent, session_id, orchestrator)
    task_agent.container.register_service("session_chat_registry", None)


@pytest.mark.asyncio
async def test_a_pre_044_DM_record_is_unchanged(room_env, monkeypatch):
    """The control: only a ROOM key moves. A DM key (or no binding at all) keeps
    the legacy restore exactly as it was — this fix must not turn old private
    sessions into rooms."""
    from core.surfaces.session_chat_registry import SessionChatRegistry

    task_agent = await _build_task_agent(room_env)
    info = await task_agent.create_session(
        "owner1", request={"task": "hello", "tools": ["browser", "filesystem", "task"]},
        session_source=SessionSource(surface_id="telegram", chat_id="42",
                                     chat_type="dm"),
        chat_session_key="agent:main:telegram:dm:42:owner1",
        skip_credit_check=True,
    )
    session_id = info["id"]
    rec = task_agent.session_manager.get_session_info(session_id)
    rec.pop("public_session", None)
    rec.pop("effective_tools", None)

    registry = SessionChatRegistry(str(room_env / "chat_dm.db"))
    registry.bind("agent:main:telegram:dm:42:owner1", session_id, "owner1",
                  "telegram", "42")
    task_agent.container.register_service("session_chat_registry", registry)
    task_agent.remove_orchestrator(session_id)

    async def _stub_llm_for_request(_request):
        return _StubLLM()

    monkeypatch.setattr(task_agent, "_get_llm_for_request", _stub_llm_for_request)

    orchestrator = await task_agent._resolve_or_recreate(session_id, rec)
    assert orchestrator is not None
    assert orchestrator._public_session is False
    assert "filesystem" in orchestrator._requested_tool_ids
    task_agent.container.register_service("session_chat_registry", None)
