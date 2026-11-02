"""095 W3 — seat filtering of blocks and schemas (SK-7, IN-12, X-16, X-25).

A room or a correspondent's session is not sent a schema, a block or a skill it
can only be refused: one predicate per block and schema, the gate's own.
"""
import types

import pytest

from core.surfaces.envelopes import SessionSource
from modules.llm.messages import MessageOrigin
from tests.unit.agents.task.test_room_session_production_path import (  # noqa: F401
    _ENV_MARKER, _StubLLM, _build_task_agent, _create_room_session, _foundation_origins,
    room_env,
)

_CTX = types.SimpleNamespace(user_id="owner1", role="orchestrator")


def _schema_names(controller):
    return {(s.get("function") or s).get("name")
            for s in controller.get_all_actions_for_provider("openai")}


async def _correspondent_session(task_agent):
    from agents.task.tool_defaults import correspondent_session_tools
    info = await task_agent.create_session(
        "owner1", request={"task": "[conversation-resume] a correspondent wrote"},
        creator="correspondent", tool_ids=correspondent_session_tools(),
        skip_credit_check=True)
    return task_agent.get_orchestrator(info["id"])


@pytest.mark.asyncio
async def test_room_and_correspondent_schemas_exclude_denied_verbs(room_env):
    from core.verb_policy import ids_where
    task_agent = await _build_task_agent(room_env)

    room = task_agent.get_orchestrator(await _create_room_session(task_agent))
    agent, _, _ = await _foundation_origins(room)
    names = _schema_names(agent.controller)
    assert names, "the room exported no schema at all"
    leaked = names & ids_where(room_denied=True)
    assert not leaked, f"room ships schemas its gate refuses: {sorted(leaked)}"
    for verb in ("load_tool", "owner_ask", "propose_action", "skill_manage"):
        assert verb not in names
        # Still registered and still refused by the gate with the seat's text.
        assert await room.controller._run_pre_tool_call_hooks(verb, {}, _CTX)
    assert {"send_message", "done"} <= names

    corr = await _correspondent_session(task_agent)
    agent, origins, text = await _foundation_origins(corr)
    names = _schema_names(agent.controller)
    leaked = names & (ids_where(correspondent_blocked=True) - {"message"})
    assert not leaked, f"correspondent ships schemas its gate refuses: {sorted(leaked)}"
    from agents.task.agent.core.seat_schemas import seat_refused_actions
    assert "message" not in seat_refused_actions(agent.controller, corr), \
        "the scoped reply tool must stay"
    # SK-7: no owner-tenant host facts on a correspondent's session.
    assert MessageOrigin.ENVIRONMENT not in origins and _ENV_MARKER not in text


@pytest.mark.asyncio
async def test_an_owner_dm_keeps_every_schema(room_env):
    task_agent = await _build_task_agent(room_env)
    info = await task_agent.create_session(
        "owner1", request={"task": "hello"},
        session_source=SessionSource(surface_id="telegram", chat_id="42", chat_type="dm"),
        chat_session_key="agent:main:telegram:dm:42", skip_credit_check=True)
    orch = task_agent.get_orchestrator(info["id"])
    agent, _, _ = await _foundation_origins(orch)
    assert "agent_status" in _schema_names(agent.controller)
    assert not agent.controller.exclude_actions


def test_status_sentence_follows_the_injector_predicates():
    from agents.task.agent.core.env_context import _status_sentence
    assert _status_sentence(False, False) == []
    leaf = " ".join(_status_sentence(True, False))
    assert "agent_status" in leaf and "<live-health>" not in leaf
    assert "<live-health>" in " ".join(_status_sentence(True, True))


def test_live_health_predicate_is_off_for_leaf_room_and_correspondent(monkeypatch):
    from agents.task.agent.core.live_health import live_health_injected
    monkeypatch.setenv("LIVE_HEALTH_CONTEXT", "true")
    owner = types.SimpleNamespace(orchestrator=types.SimpleNamespace())
    leaf = types.SimpleNamespace(orchestrator=types.SimpleNamespace(), _is_sub_agent=True)
    room = types.SimpleNamespace(orchestrator=types.SimpleNamespace(_public_session=True))
    corr = types.SimpleNamespace(orchestrator=types.SimpleNamespace(_correspondent_session=True))
    assert not live_health_injected(leaf)
    assert not live_health_injected(room)
    assert not live_health_injected(corr)
    from core.config_policy import AutonomyConfig
    assert live_health_injected(owner) is bool(AutonomyConfig.live_health_context())


@pytest.mark.asyncio
async def test_room_turning_public_mid_session_pushes_a_catalog_update(room_env, monkeypatch):
    """X-16: the catalog follows the room gate's per-call PUBLIC decision."""
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    from agents.task.agent.core.runtime_catalog import refresh_tool_catalog
    task_agent = await _build_task_agent(room_env)
    info = await task_agent.create_session(
        "owner1", request={"task": "hello"},
        session_source=SessionSource(surface_id="telegram", chat_id="42", chat_type="dm"),
        chat_session_key="agent:main:telegram:dm:42", skip_credit_check=True)
    orch = task_agent.get_orchestrator(info["id"])
    agent, _, _ = await _foundation_origins(orch)
    mm = agent.message_manager
    assert mm._tool_catalog_message is not None
    assert "seat-room" not in mm._tool_catalog_source
    orch._public_session = True        # the room gate now decides PUBLIC
    refresh_tool_catalog(agent)
    body = "\n".join(str(getattr(m, "content", ""))
                     for m in mm.get_messages_for_llm(consume_ephemeral=False))
    assert "<tool-catalog-update>" in body
    update = body.split("<tool-catalog-update>", 1)[1]
    assert "seat-room" in update


def test_money_playbooks_leave_third_party_skill_indexes():
    from core.skills.catalog import gate_allows
    money = {"triggers": {"tool_ids": ["defi_trade", "defi_data"]}, "auto_activate": True}
    plain = {"triggers": {"tool_ids": ["web_fetch"]}, "auto_activate": True}
    held = ["defi_data", "task"]
    assert gate_allows(money, held, listing=True)
    assert not gate_allows(money, held, listing=True, third_party=True)
    assert gate_allows(plain, held, third_party=True)
    assert gate_allows(None, held, third_party=True)


@pytest.mark.asyncio
async def test_room_skill_index_has_no_owner_trading_skill(room_env):
    task_agent = await _build_task_agent(room_env)
    room = task_agent.get_orchestrator(await _create_room_session(task_agent))
    agent, _, text = await _foundation_origins(room)
    for sid in ("trading-mandate", "token-identity", "trade-execution", "dca"):
        assert f'id="{sid}"' not in text
    assert agent.controller._third_party_seat is True
    from agents.task.agent.skill_manager import get_skill_manager
    assert not get_skill_manager().may_load_skill(
        "trading-mandate", tool_ids=["defi_data"], third_party=True)


class _Container:
    """Serves every tool id (so the loadable branch is reached)."""
    def has_service(self, name):
        return True


def _room_controller():
    from core.security.policy_order import install_session_gates
    from core.surfaces.room_policy import make_room_gate_hook

    class _Ctl:
        container = _Container()
        _is_sub_agent = False

        def register_pre_tool_call_hook(self, hook, fail_mode="closed"):
            pass

        def list_tools(self):
            return ["task", "web_fetch"]

    ctl = _Ctl()
    install_session_gates(ctl, correspondent_hook=lambda *a: None,
                          room_hook=make_room_gate_hook(lambda: True))
    return ctl


def test_nothing_is_loadable_on_a_seat_that_refuses_load_tool():
    from tools.tool_disclosure import resolve_tool_status, seat_probe
    probe = seat_probe(_room_controller())
    st = resolve_tool_status("filesystem", container=_Container(), loaded_ids=set(),
                             seat_denies=probe)
    assert (st.status, st.reason) == ("gated", "seat-room")
    st = resolve_tool_status("filesystem", container=_Container(), loaded_ids=set())
    assert st.status == "loadable"


@pytest.mark.asyncio
async def test_tool_search_and_describe_read_the_seat_probe():
    from tools.tool_search import perform_tool_search, perform_tool_describe
    ctl = _room_controller()
    found = (await perform_tool_search(ctl, "file system")).extracted_content
    assert "filesystem" in found and "seat-room" in found
    assert 'load_tool("filesystem")' not in found
    described = (await perform_tool_describe(ctl, "filesystem")).extracted_content
    assert "gated:seat-room" in described
