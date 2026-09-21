"""D32 — a correspondent reply held by an owner pause is never lost, even when
the session that initiated the contact is gone.

The held branch resolved the tenant from the SESSION. A dead session has no
session record, so `_owner` was None, the conversation record was skipped AND
the owner notice was skipped: the third party's reply vanished with nothing
recorded anywhere.
"""
import pytest

from agents.task.task_agent_delivery import TaskAgentDeliveryMixin


class _Registry:
    def __init__(self, row):
        self._row = row
        self.asked = []

    def resolve(self, *, surface, address, thread_id=None):
        self.asked.append((surface, address))
        return self._row


class _Store:
    def __init__(self):
        self.rows = []

    def record_inbound(self, user_id, surface, address, text, **kw):
        self.rows.append((user_id, surface, address, text))


class _EvLog:
    def __init__(self):
        self.events = []

    def record(self, kind, *, user_id="", session_id="", source="", attrs=None, **kw):
        self.events.append({"kind": kind, "user_id": user_id,
                            "attrs": dict(attrs or {})})


class _Sessions:
    def get_session_info(self, session_id):
        return None          # the session is GONE


class _Container:
    def __init__(self, services):
        self._s = services

    def get_service(self, name):
        return self._s.get(name)


class _Agent(TaskAgentDeliveryMixin):
    def __init__(self, container):
        self.container = container
        self.session_manager = _Sessions()


@pytest.fixture()
def paused(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    import core.autonomy_control as ac
    ac.pause(str(tmp_path), scopes=("all",))
    yield tmp_path
    ac.resume(str(tmp_path))


@pytest.mark.asyncio
async def test_a_held_reply_for_a_dead_session_still_records_the_tenant(
        paused, monkeypatch):
    store, ev = _Store(), _EvLog()
    registry = _Registry({"user_id": "rob", "session_id": "old"})
    agent = _Agent(_Container({"conversation_store": store,
                               "correspondent_registry": registry}))
    monkeypatch.setattr("core.event_log.event_log_enabled", lambda: True)
    monkeypatch.setattr("core.event_log.get_event_log", lambda: ev)

    ok = await agent.deliver_correspondent_data(
        "dead-session", "them@acme.com", "here is the quote", surface="email")

    assert ok is False                       # held, as designed
    assert registry.asked == [("email", "them@acme.com")]
    assert store.rows and store.rows[0][0] == "rob"
    assert any(e["kind"] == "owner_notice" and e["user_id"] == "rob"
               for e in ev.events)


@pytest.mark.asyncio
async def test_a_reply_with_no_resolvable_tenant_is_reported_not_silent(
        paused, monkeypatch, caplog):
    registry = _Registry(None)               # no binding either
    agent = _Agent(_Container({"conversation_store": _Store(),
                               "correspondent_registry": registry}))
    monkeypatch.setattr("core.event_log.event_log_enabled", lambda: True)
    monkeypatch.setattr("core.event_log.get_event_log", lambda: _EvLog())

    with caplog.at_level("ERROR"):
        ok = await agent.deliver_correspondent_data(
            "dead-session", "them@acme.com", "hello", surface="email")
    assert ok is False
    assert any("NO tenant could be resolved" in r.message for r in caplog.records)
