"""031 T9: the deterministic pre-LLM stop gate."""
import pytest

from core.surfaces.owner_intent import owner_stop_intent


class _Stub:
    def __init__(self):
        self.delivered = []

    async def ensure_session_and_deliver(self, user_id, session_id, text, *, kind="comment",
                                         metadata=None):
        self.delivered.append((session_id, kind, text))
        return "delivered"


def _result(text, session_id="sid"):
    from core.surfaces.act import InboundResult
    from core.surfaces.dispatcher import RouteDecision, RouteKind
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    src = SessionSource(surface_id="telegram", chat_id="1", chat_type="dm")
    inbound = InboundMessage(text=text, identity=Identity(user_id="rob", source=src))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        kind=RouteKind.STEER, session_key="k", session_id=session_id))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_full_stop_pauses_without_running_the_agent(home):
    from surfaces.telegram.owner_intent_gate import handle_owner_intent
    from core.autonomy_control import read_state
    stub = _Stub()
    r = _result("[voice message, auto-transcribed] Stop all ghosts today, please.")
    reply = await handle_owner_intent(stub, r, owner_stop_intent(r.inbound.text), str(home))
    assert reply.startswith("⏸ Paused everything")
    st = read_state(str(home))
    assert st.paused and st.via == "telegram:voice" and st.reason == "Stop all ghosts today, please."
    assert len(stub.delivered) == 1
    sid, kind, text = stub.delivered[0]
    assert sid == "sid" and kind == "owner_directive"
    assert "ALREADY APPLIED" in text and r.inbound.text in text


@pytest.mark.asyncio
async def test_scoped_intent_passes_through_when_model_is_alive(home, monkeypatch):
    monkeypatch.setattr("core.credit_sentinel.credit_sentinel_active", lambda *a, **k: False)
    from surfaces.telegram.owner_intent_gate import handle_owner_intent
    r = _result("stop rendering endless videos")
    assert await handle_owner_intent(_Stub(), r, owner_stop_intent(r.inbound.text), str(home)) is None


@pytest.mark.asyncio
async def test_scoped_intent_falls_back_to_full_pause_when_credit_dead(home, monkeypatch):
    monkeypatch.setattr("core.credit_sentinel.credit_sentinel_active", lambda *a, **k: True)
    from surfaces.telegram.owner_intent_gate import handle_owner_intent
    from core.autonomy_control import read_state
    r = _result("stop trading")
    reply = await handle_owner_intent(_Stub(), r, owner_stop_intent(r.inbound.text), str(home))
    assert "paused everything" in reply.lower() and read_state(str(home)).scopes == ("all",)


@pytest.mark.asyncio
async def test_scoped_intent_on_a_busy_session_pauses_everything(home, monkeypatch):
    monkeypatch.setattr("core.credit_sentinel.credit_sentinel_active", lambda *a, **k: False)
    from surfaces.telegram.owner_intent_gate import handle_owner_intent
    from core.autonomy_control import read_state
    r = _result("stop trading")
    reply = await handle_owner_intent(_Stub(), r, owner_stop_intent(r.inbound.text), str(home),
                                     busy=True)
    assert "queued as busy" in reply and read_state(str(home)).paused


@pytest.mark.asyncio
async def test_resume_passes_through_when_nothing_is_paused_and_resumes_otherwise(home):
    from surfaces.telegram.owner_intent_gate import handle_owner_intent
    from core import autonomy_control as ac
    assert owner_stop_intent("continue") is None  # plain "continue" is chat, never a resume
    r = _result("resume")
    assert await handle_owner_intent(_Stub(), r, owner_stop_intent(r.inbound.text), str(home)) is None
    ac.pause(str(home), via="test")
    reply = await handle_owner_intent(_Stub(), r, owner_stop_intent(r.inbound.text), str(home))
    assert reply.startswith("▶ Autonomy RESUMED") and not ac.read_state(str(home)).paused


@pytest.mark.asyncio
async def test_non_intent_and_none_are_ignored(home):
    from surfaces.telegram.owner_intent_gate import handle_owner_intent
    r = _result("why did you stop?")
    assert await handle_owner_intent(_Stub(), r, owner_stop_intent(r.inbound.text), str(home)) is None
    assert await handle_owner_intent(_Stub(), r, None, str(home)) is None
