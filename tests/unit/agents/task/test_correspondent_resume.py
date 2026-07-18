"""E2/E6/A6 (2026-07-13 review): delivery-rail integration with ConversationStore.

- E2: every injected correspondent reply is prefixed with the durable conversation
  context (inside the untrusted frame) and recorded into the store;
- E6/A6: a reply whose originating session is dead is NOT silently dropped — a
  replacement session is created, the registry + conversation re-point to it, and
  the reply is delivered there (gated CONVERSATION_RESUME_ENABLED, default ON).
"""
import os
import tempfile
import types

import pytest

from core.surfaces.conversations import ConversationStore
from core.surfaces.correspondents import CorrespondentRegistry


class _Orch:
    def __init__(self):
        self.injected = []

    def inject_correspondent_message(self, text, source, metadata=None, *,
                                     surface=None, address=None):
        self.injected.append({"text": text, "source": source, "surface": surface,
                              "address": address, "metadata": metadata})
        return True


class _Container:
    def __init__(self, **svcs):
        self._svcs = svcs

    def get_service(self, name):
        return self._svcs.get(name)


class _SessionManager:
    def __init__(self, infos):
        self._infos = infos

    def get_session_info(self, session_id):
        return self._infos.get(session_id, {})


def _task_agent(*, infos, container, orch_map, created=None):
    from agents.task_agent_lite import TaskAgent
    ta = object.__new__(TaskAgent)
    ta.container = container
    ta.session_manager = _SessionManager(infos)
    ta._registry = types.SimpleNamespace(get=lambda sid: orch_map.get(sid))
    ta.run_calls = []

    async def _run_session(user_id, session_id=None):
        ta.run_calls.append((user_id, session_id))
        return "ok"
    ta.run_session = _run_session

    async def _resolve_or_recreate(session_id, session_info):
        return orch_map.get(session_id)
    ta._resolve_or_recreate = _resolve_or_recreate

    async def _create_session(user_id, request, *a, **k):
        created.append((user_id, request))
        return {"session_id": "new-sess"}
    if created is not None:
        ta.create_session = _create_session
    return ta


@pytest.mark.asyncio
async def test_context_prepended_and_inbound_recorded(monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    tmp = tempfile.mkdtemp()
    store = ConversationStore(os.path.join(tmp, "conv.db"))
    store.record_outbound("t1", "email", "john@acme.com", "our offer stands",
                          session_id="sess-1", now=1000.0)
    orch = _Orch()
    ta = _task_agent(infos={"sess-1": {"user_id": "t1"}},
                     container=_Container(conversation_store=store),
                     orch_map={"sess-1": orch})
    ok = await ta.deliver_correspondent_data(
        "sess-1", "john@acme.com", "accepted, send the invoice",
        metadata={"message_id": "<r9@acme>"}, surface="email")
    assert ok is True
    inj = orch.injected[0]
    assert "our offer stands" in inj["text"], "durable context must be prepended"
    assert "[new message]" in inj["text"]
    assert "accepted, send the invoice" in inj["text"]
    hist = store.history("t1", "email", "john@acme.com")
    assert hist[-1]["direction"] == "in"
    assert hist[-1]["mid"] == "<r9@acme>"


@pytest.mark.asyncio
async def test_dead_session_resumes_into_new_session(monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.delenv("CONVERSATION_RESUME_ENABLED", raising=False)
    tmp = tempfile.mkdtemp()
    store = ConversationStore(os.path.join(tmp, "conv.db"))
    store.record_outbound("t1", "email", "john@acme.com", "old outreach",
                          session_id="dead-sess", now=1000.0)
    reg = CorrespondentRegistry(os.path.join(tmp, "corr.db"))
    reg.seed(surface="email", address="john@acme.com", session_id="dead-sess",
             user_id="t1", require_approval=False)
    new_orch = _Orch()
    created = []
    ta = _task_agent(infos={},  # dead session: no metadata anywhere
                     container=_Container(conversation_store=store,
                                          correspondent_registry=reg),
                     orch_map={"new-sess": new_orch}, created=created)
    ok = await ta.deliver_correspondent_data(
        "dead-sess", "john@acme.com", "hello again, weeks later", surface="email")
    assert ok is True
    assert created and created[0][0] == "t1"
    inj = new_orch.injected[0]
    assert "old outreach" in inj["text"], "resume must carry the durable context"
    assert "hello again, weeks later" in inj["text"]
    # registry + conversation re-point to the replacement session
    assert reg.resolve(surface="email", address="john@acme.com")["session_id"] == "new-sess"
    assert store.get("t1", "email", "john@acme.com")["session_id"] == "new-sess"
    import asyncio
    await asyncio.sleep(0)  # let the detached run task start
    assert ta.run_calls == [("t1", "new-sess")]


@pytest.mark.asyncio
async def test_dead_session_drops_when_resume_disabled(monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CONVERSATION_RESUME_ENABLED", "false")
    tmp = tempfile.mkdtemp()
    reg = CorrespondentRegistry(os.path.join(tmp, "corr.db"))
    reg.seed(surface="email", address="john@acme.com", session_id="dead-sess",
             user_id="t1", require_approval=False)
    created = []
    ta = _task_agent(infos={}, container=_Container(correspondent_registry=reg),
                     orch_map={}, created=created)
    ok = await ta.deliver_correspondent_data(
        "dead-sess", "john@acme.com", "hello?", surface="email")
    assert ok is False
    assert created == []


@pytest.mark.asyncio
async def test_registry_rebind_session():
    tmp = tempfile.mkdtemp()
    reg = CorrespondentRegistry(os.path.join(tmp, "corr.db"))
    reg.seed(surface="email", address="j@x.com", session_id="s1", user_id="t1",
             require_approval=False)
    n = reg.rebind_session(surface="email", address="J@X.com", user_id="t1",
                           new_session_id="s2")
    assert n == 1
    assert reg.resolve(surface="email", address="j@x.com")["session_id"] == "s2"
