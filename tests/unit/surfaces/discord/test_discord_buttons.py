"""Order 0016 — Discord native buttons, offline.

Out: explicit OutboundMessage.actions render as message components (never
derived from text). In: an INTERACTION_CREATE button press is the authenticated
PRESSER typing the command; a payload that is not a command is dropped.
"""
import asyncio
import types

from core.surfaces.access import AccessTier, resolve_access_tier  # noqa: F401
from core.surfaces.envelopes import Action, OutboundMessage
from surfaces.discord.components import parse_interaction, render_components
from surfaces.discord.surface import DiscordSurface

TOKEN = "/approve_p_a1b2c3"


def _press(command=TOKEN, *, guild=True, user="777", iid="i1", ctype=3, comp=2):
    d = {"id": iid, "token": "itok", "type": ctype, "channel_id": "chan-1",
         "data": {"custom_id": command, "component_type": comp},
         "message": {"id": "m9"}}
    if guild:
        d["guild_id"] = "g1"
        d["member"] = {"user": {"id": user, "username": "pat"}}
    else:
        d["user"] = {"id": user, "username": "pat"}
    return d


def test_supports_actions():
    assert DiscordSurface(object()).capabilities.supports_actions is True


def test_render_components_shape_and_limits():
    acts = [Action("Approve", TOKEN, "primary"), Action("Reject", "/reject_p_a1b2c3", "danger"),
            Action("Read", "/status")]
    rows = render_components(acts)
    assert len(rows) == 1 and rows[0]["type"] == 1
    b = rows[0]["components"]
    assert [x["style"] for x in b] == [1, 4, 2]
    assert b[0] == {"type": 2, "style": 1, "label": "Approve", "custom_id": TOKEN}
    many = [Action(f"a{i}", f"/approve_p_{i:06x}") for i in range(30)]
    rows = render_components(many)
    assert 1 <= len(rows) <= 5 and all(len(r["components"]) <= 5 for r in rows)
    assert render_components([]) == []
    assert render_components(None) == []


def test_press_is_the_presser_typing_the_command():
    m = parse_interaction(_press())
    assert m.text == TOKEN
    assert m.identity.raw_user_id == "777"
    assert m.identity.source.surface_id == "discord"
    assert m.identity.source.chat_id == "chan-1"
    assert m.identity.source.chat_type == "group"
    assert m.mentions_bot is True
    assert m.idempotency_key == "card:i1"


def test_dm_press_uses_user_and_dm_tier():
    m = parse_interaction(_press(guild=False))
    assert m.identity.source.chat_type == "dm" and m.identity.raw_user_id == "777"


def test_non_command_and_non_button_dropped():
    assert parse_interaction(_press("rm -rf /")) is None
    assert parse_interaction(_press("hello there")) is None
    assert parse_interaction(_press(ctype=2)) is None      # a slash command, not a press
    assert parse_interaction(_press(comp=3)) is None       # a select menu
    assert parse_interaction({}) is None
    assert parse_interaction(_press(user="")) is None


class _Client:
    def __init__(self):
        self.sent = []

    async def send_message(self, channel_id, text, reply_to=None, components=None):
        self.sent.append((channel_id, text, components))
        return {"id": f"m{len(self.sent)}"}


def test_send_puts_components_on_last_chunk_only():
    c = _Client()
    s = DiscordSurface(c)
    res = asyncio.run(s.send(OutboundMessage(
        session_key="agent:main:discord:dm:chan-1:u_x", text="x" * 4500,
        actions=[Action("Approve", TOKEN, "primary")])))
    assert res.success
    assert [x[2] is not None for x in c.sent] == [False, False, True]
    assert c.sent[-1][2][0]["components"][0]["custom_id"] == TOKEN


def test_send_without_actions_passes_no_components():
    c = _Client()
    asyncio.run(DiscordSurface(c).send(OutboundMessage(
        session_key="agent:main:discord:dm:chan-1:u_x", text="hi")))
    assert c.sent == [("chan-1", "hi", None)]


def test_harness_press_acks_then_routes():
    from surfaces.discord.harness import DiscordHarness
    calls = []

    class C:
        async def ack_interaction(self, iid, tok):
            calls.append(("ack", iid, tok))

    h = DiscordHarness.__new__(DiscordHarness)
    h._client = C()
    h._gateway = types.SimpleNamespace(bot_user_id="bot")
    h._user_directory = None
    h._is_duplicate = lambda inbound: False

    async def _route(inbound):
        calls.append(("route", inbound.text))
    h._route = _route
    asyncio.run(h.handle_interaction(_press()))
    assert calls == [("ack", "i1", "itok"), ("route", TOKEN)]


def test_harness_dup_or_bad_press_not_routed():
    from surfaces.discord.harness import DiscordHarness
    calls = []

    class C:
        async def ack_interaction(self, iid, tok):
            calls.append("ack")

    h = DiscordHarness.__new__(DiscordHarness)
    h._client = C()
    h._gateway = types.SimpleNamespace(bot_user_id="bot")
    h._user_directory = None
    h._is_duplicate = lambda inbound: True

    async def _route(inbound):
        calls.append("route")
    h._route = _route
    asyncio.run(h.handle_interaction(_press()))
    asyncio.run(h.handle_interaction(_press("nope")))
    assert "route" not in calls and calls.count("ack") == 2   # always ack: no spinner left


def test_gateway_dispatches_interaction_create_to_its_handler():
    import surfaces.discord.gateway as gw
    from tests.unit.surfaces.discord.test_gateway_robustness import _FakeWS
    got = {"msg": [], "press": []}

    async def on_msg(d):
        got["msg"].append(d)

    async def on_press(d):
        got["press"].append(d)

    async def go():
        client = gw.DiscordGatewayClient("tok", lambda: "url")
        client._on_interaction = on_press
        ws = _FakeWS([{"op": 0, "t": "INTERACTION_CREATE", "s": 1, "d": _press()}])
        await client._consume(ws, on_msg)
        await asyncio.gather(*list(client._handler_tasks))

    asyncio.run(go())
    assert got["msg"] == [] and got["press"][0]["id"] == "i1"
