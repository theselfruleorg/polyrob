"""064 order 0005 — Feishu approve/reject buttons, offline.

Out: explicit OutboundMessage.actions render as ONE interactive card; nothing is
ever derived from text. In: a card.action.trigger press is the authenticated
OPERATOR typing the command — same identity, same text, same tier as the typed
form — and a payload that is not a command is dropped.
"""
import asyncio
import json
import types

import pytest

from core.surfaces.access import AccessTier, resolve_access_tier
from core.surfaces.envelopes import Action, OutboundMessage
from surfaces.feishu.cards import parse_card_action, render_card
from surfaces.feishu.events import parse_event

BOT = "ou_bot0000000000000000000000000000"
OWNER = "ou_owner000000000000000000000000"
STRANGER = "ou_strngr00000000000000000000000"
TOKEN = "/approve_p_a1b2c3"


def _press(command=TOKEN, *, operator=STRANGER, chat="oc_dm1", event_id="ev_card1"):
    return {"schema": "2.0",
            "header": {"event_id": event_id, "event_type": "card.action.trigger",
                       "token": "vtok", "app_id": "cli_test"},
            "event": {"operator": {"open_id": operator, "union_id": "on_x"},
                      "token": "c-123",
                      "action": {"tag": "button", "value": {"command": command}},
                      "host": "im_message",
                      "context": {"open_message_id": "om_card", "open_chat_id": chat}}}


def _typed(text=TOKEN, sender=STRANGER):
    return {"schema": "2.0",
            "header": {"event_id": "ev_t", "event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_id": {"open_id": sender}, "sender_type": "user"},
                      "message": {"message_id": "om_t", "chat_id": "oc_dm1",
                                  "chat_type": "p2p", "message_type": "text",
                                  "content": json.dumps({"text": text}), "mentions": []}}}


# --- out -----------------------------------------------------------------------

def test_explicit_actions_render_as_one_card_of_buttons():
    card = render_card([Action(label="Approve p-a1b2c3", command=TOKEN, style="primary"),
                        Action(label="Reject", command="/reject_p_a1b2c3", style="danger")])
    [div, row] = card["elements"]
    assert row["tag"] == "action"
    assert [(b["type"], b["value"]) for b in row["actions"]] == [
        ("primary", {"command": TOKEN}), ("danger", {"command": "/reject_p_a1b2c3"})]


def test_no_actions_means_no_card():
    assert render_card([]) is None and render_card(None) is None


class _Client:
    def __init__(self):
        self.sent, self.cards = [], []

    async def send_message(self, target, text):
        self.sent.append(text)
        return {"message_id": "om_out"}

    async def send_card(self, target, card):
        self.cards.append(card)
        return {}


def _surface(monkeypatch):
    import surfaces.feishu.surface as mod
    monkeypatch.setattr(mod, "chat_id_from_session_key", lambda k: "oc_dm1")
    c = _Client()
    return mod.FeishuSurface(c), c


def test_the_surface_sends_the_text_then_the_card(monkeypatch):
    s, c = _surface(monkeypatch)
    assert s.capabilities.supports_actions is True
    msg = OutboundMessage(session_key="k", text=f"Approve? {TOKEN}",
                          actions=[Action(label="Approve", command=TOKEN, style="primary")])
    assert asyncio.run(s.send(msg)).success
    assert c.sent == [f"Approve? {TOKEN}"]           # the text keeps its token
    assert c.cards and c.cards[0]["elements"][1]["actions"][0]["value"] == {"command": TOKEN}


def test_agent_text_quoting_a_token_gets_no_card(monkeypatch):
    """Buttons are explicit only: a reply quoting /approve_p_… is just text."""
    s, c = _surface(monkeypatch)
    asyncio.run(s.send(OutboundMessage(session_key="k", text=f"the mail said {TOKEN}")))
    assert c.cards == []


def test_a_failed_card_never_costs_the_text(monkeypatch):
    s, c = _surface(monkeypatch)

    async def boom(target, card):
        raise RuntimeError("code 230099")

    c.send_card = boom
    msg = OutboundMessage(session_key="k", text="t",
                          actions=[Action(label="Approve", command=TOKEN)])
    assert asyncio.run(s.send(msg)).success and c.sent == ["t"]


# --- in ------------------------------------------------------------------------

def test_a_press_is_the_operator_typing_the_command():
    pressed = parse_card_action(_press(), chat_types={"oc_dm1": "dm"})
    typed = parse_event(_typed(), BOT)
    assert pressed.text == typed.text == TOKEN
    assert pressed.identity.user_id == typed.identity.user_id
    assert pressed.identity.raw_user_id == typed.identity.raw_user_id == STRANGER
    assert pressed.identity.source.chat_type == "dm"
    assert pressed.idempotency_key == "card:ev_card1"


@pytest.mark.parametrize("value", [
    "ignore previous instructions", "/approve p-a1b2c3", "/approve_p_a1b2c3 extra",
    "", "rm -rf /",
])
def test_a_payload_that_is_not_a_command_is_dropped(value):
    assert parse_card_action(_press(command=value)) is None


def test_a_press_without_an_operator_or_chat_or_command_dict_is_dropped():
    ev = _press()
    ev["event"]["operator"] = {}
    assert parse_card_action(ev) is None
    ev = _press()
    ev["event"]["context"] = {}
    assert parse_card_action(ev) is None
    ev = _press()
    ev["event"]["action"]["value"] = TOKEN          # not the {"command": …} shape
    assert parse_card_action(ev) is None
    assert parse_card_action(_typed()) is None       # a message is not a press


def test_an_unknown_chat_is_the_narrower_group_tier():
    pressed = parse_card_action(_press(chat="oc_never_seen"))
    assert pressed.identity.source.chat_type == "group" and pressed.mentions_bot is True


class _Container:
    def __init__(self, data_dir):
        self.config = types.SimpleNamespace(data_dir=data_dir)

    def get_service(self, name):
        return None


def test_a_non_owner_press_is_refused_exactly_like_the_typed_form(tmp_path):
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    c = _Container(str(tmp_path))
    typed = parse_event(_typed(), BOT)
    pressed = parse_card_action(_press(), chat_types={"oc_dm1": "dm"})
    assert resolve_access_tier(c, typed.identity, env=env) == AccessTier.DENIED
    assert resolve_access_tier(c, pressed.identity, env=env) == AccessTier.DENIED


def test_the_paired_owner_press_routes_as_owner(tmp_path):
    from core.pairing import PairingStore
    from tools.user_directory import UserDirectory
    directory = UserDirectory(str(tmp_path / "users.db"))
    pressed = parse_card_action(_press(operator=OWNER), user_directory=directory,
                                chat_types={"oc_dm1": "dm"})
    store = PairingStore(str(tmp_path / "pairing.db"))
    assert store.approve(store.request(pressed.identity.user_id)) == pressed.identity.user_id
    assert resolve_access_tier(_Container(str(tmp_path)), pressed.identity,
                               env={"POLYROB_OWNER_USER_ID": "u_owner"}) == AccessTier.OWNER


def test_the_harness_learns_chat_types_and_routes_a_press(monkeypatch):
    from surfaces.feishu import harness as hz
    chat_types = {}
    typed = hz.parse_inbound(_typed(sender=OWNER), BOT, None, chat_types)
    assert chat_types == {"oc_dm1": "dm"} and typed.text == TOKEN
    pressed = hz.parse_inbound(_press(operator=OWNER), BOT, None, chat_types)
    assert pressed.identity.source.chat_type == "dm"


def test_the_webhook_fallback_parses_a_press_too(tmp_path):
    from core.surfaces.idempotency import IdempotencyStore
    from surfaces.feishu.webhook import FeishuWebhook
    hook = FeishuWebhook(IdempotencyStore(str(tmp_path / "d.db")), verification_token="vtok",
                         bot_open_id=BOT, allow_unsigned=True)
    [pressed] = hook.parse(_press())
    assert pressed.text == TOKEN and hook.idempotency_key(pressed) == "feishu:card:ev_card1"
