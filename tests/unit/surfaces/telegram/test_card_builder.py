"""Build a transaction from buttons: bare /send (or /swap) answers with a
builder card — chain, token, recipient, amount — each a row of buttons on the
same card; the last tap runs the verb's QUOTE, which answers with the quote
card whose Confirm is the only thing that moves money."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.surfaces import cards
from core.surfaces.command_reply import CommandReply, reply_text
from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces import card_builder
from surfaces.telegram import harness
from surfaces.telegram.inbound import InboundResult

ADDR = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


class _Gate:
    per_tx_cap_usd = 3000.0
    daily_cap_usd = 7000.0

    def rolling_24h_spend_usd(self, venue=None):
        return 0.0


class _Tool:
    def __init__(self):
        self.calls = []

    def _get_wallet(self):
        return SimpleNamespace(policy=_Gate(),
                               operational_signer=lambda: SimpleNamespace(address=ADDR),
                               solana_address="SoLAddr")

    async def transfer(self, params, ctx):
        self.calls.append(params)
        body = ("transfer\n  simulated value: $100.0000\n"
                + ("  RESULT: DRY RUN (simulation only)" if params.dry_run
                   else "  RESULT: SENT AND CONFIRMED\n  tx: 0xabc"))
        return SimpleNamespace(error=None, extracted_content=body)


def _cmd(text, surface="telegram"):
    source = SessionSource(surface_id=surface, chat_id="1", chat_type="dm")
    inbound = InboundMessage(text=text, identity=Identity(user_id="alice", source=source))
    return InboundResult(inbound=inbound,
                         decision=RouteDecision(kind=RouteKind.COMMAND, session_key="telegram:1",
                                                session_id=None, command=text.split()[0]))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    tool = _Tool()
    monkeypatch.setattr("tools.defi.trade_tool.DefiTradeTool", lambda: tool)
    monkeypatch.setattr(card_builder, "_chains", lambda verb: ["base", "ethereum"])
    monkeypatch.setattr(card_builder, "_trusted", lambda uid, chain: [("USDC", USDC)])
    monkeypatch.setattr(card_builder, "_recipients", lambda uid, chain: [("0x2FAa…f0e7", ADDR)])
    monkeypatch.setattr(card_builder, "_balance", lambda w, chain, token: Decimal("2"))
    agent = SimpleNamespace(container=SimpleNamespace(
        config=SimpleNamespace(data_dir=str(tmp_path)), get_service=lambda n: None))
    return SimpleNamespace(tool=tool, agent=agent)


def _run(env, text, surface="telegram"):
    return asyncio.run(harness.act_on_inbound(env.agent, _cmd(text, surface)))


def test_send_is_built_with_buttons_then_quoted_then_confirmed(env):
    start = _run(env, "/send")
    assert isinstance(start, CommandReply) and start.card_id
    cid = start.card_id
    card = cards.store().get(cid)
    assert card.kind == cards.KIND_BUILDER and card.options == ["base", "ethereum"]
    assert [a.label for a in cards.card_actions(card)][-1] == "Cancel"

    assert _run(env, f"/card_{cid}_1") is None             # chain: base (edited in place)
    assert cards.store().get(cid).options == ["native", "USDC"]
    assert _run(env, f"/card_{cid}_1") is None             # token: native
    assert cards.store().get(cid).options == ["0x2FAa…f0e7"]
    assert _run(env, f"/card_{cid}_1") is None             # recipient
    assert cards.store().get(cid).options == ["25% · 0.5", "50% · 1", "90% · 1.8"]
    assert not env.tool.calls                              # nothing ran yet

    quote = _run(env, f"/card_{cid}_2")                    # amount: 1 -> the QUOTE runs
    assert isinstance(quote, CommandReply) and quote.card_id != cid
    assert [p.dry_run for p in env.tool.calls] == [True]   # a quote, nothing sent
    q = cards.store().get(quote.card_id)
    assert q.confirm_line == f"/send 1 native to {ADDR} on base max 105.01 go"
    assert cards.store().get(cid).state == cards.S_REPLACED

    sent = _run(env, f"/card_{quote.card_id}_ok")          # the ONLY step that moves money
    assert "SENT AND CONFIRMED" in reply_text(sent)
    assert [p.dry_run for p in env.tool.calls] == [True, True, False]   # confirm re-simulates, then sends


def test_two_taps_on_one_step_advance_once(env):
    cid = _run(env, "/send").card_id
    stale = cards.store().get(cid)
    card_builder.advance(stale, 1, "alice")
    again = card_builder.advance(stale, 2, "alice")        # the same snapshot
    assert "moved on" in again.text
    assert cards.store().get(cid).draft["chain"] == "base"


def test_a_step_with_nothing_to_pick_shows_the_line_to_type(env, monkeypatch):
    monkeypatch.setattr(card_builder, "_recipients", lambda uid, chain: [])
    cid = _run(env, "/send").card_id
    _run(env, f"/card_{cid}_1")
    _run(env, f"/card_{cid}_1")
    card = cards.store().get(cid)
    assert card.options == [] and "/send <amount> native to <address> on base" in card.body


def test_cancel_and_other_owners(env):
    cid = _run(env, "/send").card_id
    assert "Cancelled" in reply_text(_run(env, f"/card_{cid}_no"))
    cid = _run(env, "/send").card_id
    assert cards.press(cid, "1", "mallory").reply.startswith("No such card")


def test_the_console_prints_the_next_step(env):
    cid = _run(env, "/send", surface="webview").card_id
    text = _run(env, f"/card_{cid}_1", surface="webview")
    assert f"/card_{cid}_1" in text and "native" in text


def test_the_repl_builds_the_same_way(env):
    from cli.ui.commands.handlers import build_default_registry
    from cli.ui.commands.registry import CommandContext
    reg = build_default_registry()
    out = []
    ctx = CommandContext(user_id="alice", registry=reg)
    ctx.emit = lambda text, **kw: out.append(text)
    asyncio.run(reg.dispatch("/send", ctx))
    (card,) = cards.store().open_cards("alice")
    assert card.kind == cards.KIND_BUILDER and f"/card_{card.card_id}_1" in out[-1]
    for pick in ("1", "1", "1", "2"):
        asyncio.run(reg.dispatch(f"/card_{card.card_id}_{pick}", ctx))
    assert any("max 105.01 go" in o for o in out)          # the quote card, typed tokens
