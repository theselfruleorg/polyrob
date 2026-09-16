"""044 T21: a ROOM is not a correspondent.

`message(surface="telegram", target="<room chat id>")` used to run the whole
third-party rail: the outbound tier ladder DENIED it (a room chat id was never on
the OUTBOUND allowlist), and had it passed, `maybe_seed_correspondent` would have
registered the room as a person the agent is corresponding with and charged it to
the per-day correspondent cap.

A room is neither. It is a chat the OWNER allowlisted for the agent's presence,
and its bound is the room's own hourly reply cap — the same `RoomCaps` the live
reply path (`MessageRouter.publish`) applies, so a post cannot dodge the cap by
going through the `message` tool instead.
"""
import asyncio

import pytest

from core.surfaces.group_allowlist import GroupAllowlist
from core.surfaces.room_caps import RoomCaps
from tools.controller import message_send as ms


class _Router:
    def __init__(self, ok=True):
        self.sent = []
        self._ok = ok

    async def send_message(self, chat_id, text, surface_id="telegram", media=None):
        self.sent.append((surface_id, chat_id, text))
        return self._ok

    def capabilities(self, surface_id):
        return None


class _Container:
    def __init__(self, tmp_path):
        import types
        self.config = types.SimpleNamespace(data_dir=str(tmp_path))
        self._svc = {"room_caps": RoomCaps(str(tmp_path / "surfaces.db"))}

    def get_service(self, n):
        return self._svc.get(n)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("telegram", "-1001")

    def _explode(*a, **kw):
        raise AssertionError("a room must never be seeded as a correspondent")

    monkeypatch.setattr("core.surfaces.seed.maybe_seed_correspondent", _explode)
    return _Container(tmp_path), _Router()


def _send(router, container, *, target="-1001", text="hi"):
    return asyncio.run(ms.perform_message_send(
        router=router, allowlist=None, owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target=target, text=text,
        session_id="s1", container=container, execution_context=None,
        controller=None))


def test_a_room_target_sends_without_a_correspondent_seed(rig):
    container, router = rig
    res = _send(router, container)
    assert res["success"] is True, res
    assert res["tier"] == "room"
    assert router.sent == [("telegram", "-1001", "hi")]
    assert container.get_service("room_caps").replies_since("telegram", "-1001", 0) == 1


def test_the_room_hourly_cap_is_enforced(rig, monkeypatch):
    container, router = rig
    monkeypatch.setenv("GROUP_REPLY_CAP_PER_HOUR", "1")
    assert _send(router, container)["success"] is True
    res = _send(router, container, text="again")
    assert res["success"] is False
    assert "cap" in (res["error"] or "").lower()
    assert len(router.sent) == 1, "an over-cap post must never reach the surface"


def test_a_failed_send_never_consumes_the_cap(rig):
    container, _router = rig
    router = _Router(ok=False)
    res = _send(router, container)
    assert res["success"] is False
    assert container.get_service("room_caps").replies_since("telegram", "-1001", 0) == 0


def test_a_room_is_exempt_from_the_open_tier_daily_cap(rig, monkeypatch):
    """The daily send cap bounds CONTACTING STRANGERS. A room the owner put the
    agent in is not a stranger, and it already has its own hourly cap."""
    container, router = rig
    monkeypatch.setenv("OUTBOUND_POLICY", "open")
    monkeypatch.setenv("OUTBOUND_DAILY_SEND_CAP", "0")
    res = _send(router, container)
    assert res["success"] is True, res
    assert res["tier"] == "room"


def test_a_non_allowlisted_chat_id_is_still_denied(rig):
    container, router = rig
    res = _send(router, container, target="-2002")
    assert res["success"] is False and res["tier"] == "denied"
    assert router.sent == []


def test_the_owner_target_is_never_treated_as_a_room(rig, monkeypatch):
    """Polarity guard: if the owner's own DM id were ever allowlisted as a room,
    an owner ping must still resolve `owner` (it is exempt from the room cap and
    from the seed rail for different reasons)."""
    container, router = rig
    GroupAllowlist(str(container.config.data_dir) + "/group_allowlist.db").allow(
        "telegram", "999")
    res = _send(router, container, target="999")
    assert res["tier"] == "owner"


# ---------------------------------------------------------------------------
# 044 I6 — a room post through the `message` tool obeys the ROOM delivery rules
# ---------------------------------------------------------------------------

def test_a_room_post_is_secret_scrubbed(rig):
    """`MessageRouter.publish` scrubs secret shapes out of every room REPLY
    because the audience is many humans. A post through the `message` tool went
    straight to `router.send_message` and skipped it — so the one verb that can
    be aimed at an arbitrary chat was the one that bypassed the public scrub."""
    container, router = rig
    leak = "here is the key sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD ok"
    res = _send(router, container, text=leak)
    assert res["success"] is True, res
    delivered = router.sent[0][2]
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD" not in delivered
    assert delivered != leak


def test_a_room_post_of_SILENT_costs_nothing(rig):
    """`[SILENT]` is the agent's 'nothing here needs an answer'. In a room that
    must cost NO message (the same exact-match rule `publish` applies), or a
    judgement of silence becomes a public non-sequitur."""
    container, router = rig
    res = _send(router, container, text="[SILENT]")
    assert res["success"] is True and res.get("suppressed") is True
    assert router.sent == [], "[SILENT] was posted into the room"
    assert container.get_service("room_caps").replies_since("telegram", "-1001", 0) == 0


def test_SILENT_to_a_NON_room_target_is_unchanged(rig):
    """The rule is about the AUDIENCE, not the token: a DM keeps legacy behaviour."""
    container, router = rig
    res = _send(router, container, target="999", text="[SILENT]")
    assert res["tier"] == "owner" and res["success"] is True
    assert router.sent == [("telegram", "999", "[SILENT]")]


def test_a_clean_room_post_is_delivered_byte_identical(rig):
    container, router = rig
    _send(router, container, text="the bridge is live on Base")
    assert router.sent[0][2] == "the bridge is live on Base"
