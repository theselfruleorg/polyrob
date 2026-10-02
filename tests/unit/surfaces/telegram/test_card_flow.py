"""Action cards on the owner seats: a `/send` quote becomes a card; its Confirm
runs the exact line the quote showed, once, through the seat's own gates."""
import asyncio
from types import SimpleNamespace

import pytest

from core.surfaces import cards
from core.surfaces.command_reply import CommandReply, reply_text
from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram import harness
from surfaces.telegram.inbound import InboundResult

ADDR = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"


class _Gate:
    per_tx_cap_usd = 3000.0
    daily_cap_usd = 7000.0

    def rolling_24h_spend_usd(self, venue=None):
        return 0.0


class _Tool:
    def __init__(self, usd=100.0):
        self.calls = []
        self.usd = usd

    def _get_wallet(self):
        return SimpleNamespace(policy=_Gate())

    async def transfer(self, params, ctx):
        self.calls.append((params, ctx))
        if params.dry_run:
            body = (f"transfer\n  simulated value: ${self.usd:.4f}\n"
                    "  RESULT: DRY RUN (simulation only)")
        else:
            body = "transfer\n  RESULT: SENT AND CONFIRMED\n  tx: 0xabc"
        return SimpleNamespace(error=None, extracted_content=body)


class _Container:
    def __init__(self, data_dir):
        self.config = SimpleNamespace(data_dir=data_dir)

    def get_service(self, name):
        return None


def _agent(data_dir):
    return SimpleNamespace(container=_Container(data_dir))


def _cmd(text, user_id="alice", key="telegram:1"):
    source = SessionSource(surface_id="telegram", chat_id="1", chat_type="dm")
    inbound = InboundMessage(text=text, identity=Identity(user_id=user_id, source=source))
    decision = RouteDecision(kind=RouteKind.COMMAND, session_key=key,
                             session_id=None, command=text.split()[0])
    return InboundResult(inbound=inbound, decision=decision)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    tool = _Tool()
    import tools.defi.trade_tool as tt
    monkeypatch.setattr(tt, "DefiTradeTool", lambda: tool)
    return SimpleNamespace(dir=str(tmp_path), tool=tool)


def _run(agent, text, **kw):
    return asyncio.run(harness.act_on_inbound(agent, _cmd(text, **kw)))


def test_quote_is_a_card_and_confirm_sends_the_quoted_line_once(env):
    agent = _agent(env.dir)
    reply = _run(agent, f"/send 1 native to {ADDR} on ethereum")
    assert isinstance(reply, CommandReply) and reply.card_id
    card = cards.store().get(reply.card_id)
    assert card.confirm_line == f"/send 1 native to {ADDR} on ethereum max 105.01 go"
    assert f"/card_{card.card_id}_ok" in reply.text
    assert [p.dry_run for p, _ in env.tool.calls] == [True]

    out = _run(agent, f"/card_{card.card_id}_ok")
    assert "SENT" in reply_text(out)
    live = [p for p, _ in env.tool.calls if not p.dry_run]
    assert len(live) == 1 and live[0].max_spend_usd == pytest.approx(105.01)
    assert live[0].to == ADDR                       # the stored address, not re-typed
    assert cards.store().get(card.card_id).state == cards.S_DONE

    again = _run(agent, f"/card_{card.card_id}_ok")
    assert "already decided" in reply_text(again)
    assert len([p for p, _ in env.tool.calls if not p.dry_run]) == 1


def test_price_moved_above_the_confirmed_bound_sends_nothing(env):
    agent = _agent(env.dir)
    reply = _run(agent, f"/send 1 native to {ADDR} on ethereum")
    env.tool.usd = 200.0
    out = _run(agent, f"/card_{reply.card_id}_ok")
    assert "price moved" in reply_text(out)
    assert not [p for p, _ in env.tool.calls if not p.dry_run]
    assert cards.store().get(reply.card_id).state == cards.S_FAILED


def test_a_non_owner_tap_runs_nothing(env):
    agent = _agent(env.dir)
    reply = _run(agent, f"/send 1 native to {ADDR} on ethereum")
    out = _run(agent, f"/card_{reply.card_id}_ok", user_id="mallory")
    assert "Owner only" in reply_text(out)
    assert cards.store().get(reply.card_id).state == cards.S_OPEN


def test_cards_is_refused_in_a_room():
    assert harness._room_refused("/cards")


def test_refresh_makes_a_new_card_and_retires_the_old(env):
    agent = _agent(env.dir)
    first = _run(agent, f"/send 1 native to {ADDR} on ethereum")
    second = _run(agent, f"/card_{first.card_id}_re")
    assert isinstance(second, CommandReply) and second.card_id != first.card_id
    assert cards.store().get(first.card_id).state == cards.S_REPLACED


def test_cards_lists_open_cards(env):
    agent = _agent(env.dir)
    _run(agent, f"/send 1 native to {ADDR} on ethereum")
    assert "Open cards" in reply_text(_run(agent, "/cards"))


def test_bad_checksum_is_refused_before_any_quote(env):
    bad = ADDR[:-1] + ("E" if ADDR[-1] != "E" else "F")
    agent = _agent(env.dir)
    out = reply_text(_run(agent, f"/send 1 native to {bad} on ethereum"))
    assert "checksum" in out and not env.tool.calls


def test_first_send_warning_until_a_card_sent_there(env):
    agent = _agent(env.dir)
    first = _run(agent, f"/send 1 native to {ADDR} on ethereum")
    assert "first send to this address" in first.text
    _run(agent, f"/card_{first.card_id}_ok")
    second = _run(agent, f"/send 1 native to {ADDR} on ethereum")
    assert "first send to this address" not in second.text


def test_repl_tap_token_confirms_the_same_card(env, monkeypatch):
    from cli.ui.commands.registry import CommandContext
    from cli.ui.commands.handlers import build_default_registry
    reg = build_default_registry()
    out = []
    ctx = CommandContext(user_id="alice", registry=reg)
    monkeypatch.setattr(ctx, "emit", lambda text, **kw: out.append(text), raising=False)
    asyncio.run(reg.dispatch(f"/send 1 native to {ADDR} on ethereum", ctx))
    card = [c for c in cards.store().open_cards("alice")][0]
    assert f"/card_{card.card_id}_ok" in out[-1]
    asyncio.run(reg.dispatch(f"/card_{card.card_id}_ok", ctx))
    assert any("SENT" in o for o in out)
    assert cards.store().get(card.card_id).state == cards.S_DONE


class _Bot:
    def __init__(self):
        self.edits = []

    async def edit_message_reply_markup(self, **kw):
        self.edits.append(kw)


def _tap_update(callback_of=77):
    return {"update_id": 1, "message": {"message_id": None, "callback_of": callback_of,
                                        "chat": {"id": 5}, "text": "/approve_p_abc"}}


def test_a_handled_tap_retires_its_buttons():
    from surfaces.telegram.card_ops import retire_tapped_buttons
    bot = _Bot()
    assert asyncio.run(retire_tapped_buttons(bot, _tap_update(), "✅ Approved."))
    assert bot.edits == [{"chat_id": 5, "message_id": 77, "reply_markup": None}]


@pytest.mark.parametrize("reply", ["🔒 Owner only.", "Command failed: boom"])
def test_a_refused_tap_keeps_the_buttons(reply):
    from surfaces.telegram.card_ops import retire_tapped_buttons
    bot = _Bot()
    assert not asyncio.run(retire_tapped_buttons(bot, _tap_update(), reply))
    assert not asyncio.run(retire_tapped_buttons(bot, {"message": {"text": "/x"}}, "ok"))
    assert bot.edits == []


def test_the_usage_text_lists_recent_recipients_after_a_confirmed_card(env):
    """Bare /send on a seat is the button builder now; the usage text (the
    fallback when there is no wallet) still lists the recipients in full."""
    from surfaces.telegram import send_ops
    agent = _agent(env.dir)
    assert "Recent recipients" not in asyncio.run(send_ops.send_reply("alice", []))
    first = _run(agent, f"/send 1 native to {ADDR} on ethereum")
    _run(agent, f"/card_{first.card_id}_ok")
    assert ADDR in asyncio.run(send_ops.send_reply("alice", []))
    assert cards.store().recent_recipients("alice") == [ADDR]


def test_the_owner_thread_keeps_sends_swaps_and_card_taps():
    assert {"/send", "/swap"} <= harness._THREAD_COMMANDS


def test_the_console_form_and_the_builder_offer_the_same_chains():
    from surfaces.card_builder import chain_choices
    assert "solana" in chain_choices("/send")
