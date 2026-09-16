"""044 T21: a cron job delivering into a ROOM obeys the room's rules.

`deliver_target=<room chat id>` used to go down the owner-DM rail
(`deliver_user_message`), whose dedup and daily caps are keyed to the OWNER's
private notice budget — the wrong bound entirely for a public room, and one that
would let a cron report bypass `GROUP_REPLY_CAP_PER_HOUR` completely.

A room target now routes through the router with the room's own hourly cap and
the SAME secret re-scrub `MessageRouter.publish` applies to a room reply (a room
is many humans; a secret shape that survived every other scrub must not be the
thing the agent posts publicly).

The explicit-target gate is UNCHANGED: without
`CRON_DELIVERY_ALLOW_EXPLICIT_TARGET` an agent-supplied target is still ignored
in favour of the owner's own channel.
"""
import pytest

from core.surfaces.group_allowlist import GroupAllowlist
from core.surfaces.room_caps import RoomCaps
from cron import delivery
from cron.jobs import CronJob


def _job(payload=None):
    return CronJob(id="j-room", task="report", schedule_spec="30m", user_id="rob",
                   next_run_at=None, one_shot=False, skip_memory=True,
                   max_duration_seconds=180, payload=payload or {}, created_at=None)


class _Router:
    def __init__(self, ok=True):
        self.sent = []
        self._ok = ok

    async def send_message(self, chat_id, text, surface_id="telegram", media=None):
        self.sent.append((surface_id, chat_id, text))
        return self._ok


class _Container:
    def __init__(self, tmp_path, router):
        import types
        self.config = types.SimpleNamespace(data_dir=str(tmp_path))
        self._svc = {"message_router": router,
                     "room_caps": RoomCaps(str(tmp_path / "surfaces.db"))}

    def get_service(self, n):
        return self._svc.get(n)


class _Agent:
    def __init__(self, container):
        self.container = container
        self.config = None


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CRON_DELIVERY_ALLOW_EXPLICIT_TARGET", "true")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("telegram", "-1001")
    router = _Router()
    container = _Container(tmp_path, router)

    async def _never(*a, **kw):
        raise AssertionError("a room delivery must not use the owner-DM rail")

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _never)
    return _Agent(container), router, container


@pytest.mark.asyncio
async def test_a_room_target_goes_through_the_router_and_the_room_cap(rig):
    agent, router, container = rig
    ok = await delivery.deliver_result(agent, _job(), "the report",
                                       target="telegram", deliver_target="-1001")
    assert ok is True
    assert router.sent == [("telegram", "-1001", "the report")]
    assert container.get_service("room_caps").replies_since("telegram", "-1001", 0) == 1


@pytest.mark.asyncio
async def test_the_room_hourly_cap_suppresses_an_over_cap_report(rig, monkeypatch):
    agent, router, _c = rig
    monkeypatch.setenv("GROUP_REPLY_CAP_PER_HOUR", "1")
    assert await delivery.deliver_result(agent, _job(), "one", target="telegram",
                                         deliver_target="-1001") is True
    assert await delivery.deliver_result(agent, _job(), "two", target="telegram",
                                         deliver_target="-1001") is False
    assert len(router.sent) == 1


@pytest.mark.asyncio
async def test_a_secret_shape_is_scrubbed_before_it_reaches_the_room(rig):
    agent, router, _c = rig
    body = "done; key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    await delivery.deliver_result(agent, _job(), body, target="telegram",
                                  deliver_target="-1001")
    assert router.sent, "the report never went out"
    assert "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" \
        not in router.sent[0][2]


@pytest.mark.asyncio
async def test_a_failed_send_never_consumes_the_room_cap(rig, tmp_path):
    agent, _router, container = rig
    container._svc["message_router"] = _Router(ok=False)
    ok = await delivery.deliver_result(agent, _job(), "x", target="telegram",
                                       deliver_target="-1001")
    assert ok is False
    assert container.get_service("room_caps").replies_since("telegram", "-1001", 0) == 0


@pytest.mark.asyncio
async def test_silent_still_suppresses_a_room_report(rig):
    agent, router, _c = rig
    ok = await delivery.deliver_result(agent, _job(), "[SILENT]", target="telegram",
                                       deliver_target="-1001")
    assert ok is False and router.sent == []


@pytest.mark.asyncio
async def test_the_explicit_target_gate_is_unchanged(tmp_path, monkeypatch):
    """Without the opt-in, an agent-supplied target is still ignored — a room id
    is no exception, so this closes no exfiltration path."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CRON_DELIVERY_ALLOW_EXPLICIT_TARGET", raising=False)
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("telegram", "-1001")
    router = _Router()
    agent = _Agent(_Container(tmp_path, router))
    seen = {}

    async def _rail(container, user_id, text, source=None, recipient_override=None):
        seen["to"] = recipient_override
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _rail)
    monkeypatch.setattr(delivery, "_owner_telegram", lambda *a, **kw: "999")
    await delivery.deliver_result(agent, _job(), "x", target="telegram",
                                  deliver_target="-1001")
    assert seen["to"] == "999", "an explicit target must still be dropped by default"
    assert router.sent == []
