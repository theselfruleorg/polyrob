"""`/send` — the owner's send, quote then `go` (owner-UX review, 2026-09-26)."""
import asyncio
from types import SimpleNamespace

import pytest

from surfaces.telegram import send_ops

ADDR = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"
TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def test_parse_native_send():
    order, err = send_ops.parse(["0.997", "native", "to", ADDR, "on", "ethereum", "go"])
    assert err is None
    assert order == {"amount": 0.997, "usd": None, "token": "native", "to": ADDR,
                     "chain": "ethereum", "go": True, "max_usd": None, "anyway": False}


def test_parse_token_send_any_word_order():
    order, err = send_ops.parse(["on", "base", "25", TOKEN, "to", ADDR])
    assert err is None and order["token"] == TOKEN and order["go"] is False


@pytest.mark.parametrize("args,needle", [
    (["1", "native", "to", ADDR], "chain"),                    # no chain: never guessed
    (["1", "native", "on", "base"], "recipient"),
    (["x", "native", "to", ADDR, "on", "base"], "positive"),
    (["1", "USDC", "to", ADDR, "on", "base"], "ticker"),       # a ticker is never enough
    (["1", "native", "to", "0x123", "on", "base"], "0x address"),
])
def test_parse_refuses_what_it_cannot_prove(args, needle):
    order, err = send_ops.parse(args)
    assert order is None and needle in err


class _Gate:
    per_tx_cap_usd = 3000.0
    daily_cap_usd = 7000.0

    def rolling_24h_spend_usd(self, venue=None):
        return 1000.0


def _facts(usd, params, **kw):
    """The typed facts the send rails attach (125 S1)."""
    return dict({"simulated_usd": usd, "sent_usd": None, "unverified": False,
                 "allowed": True, "dry_run": params.dry_run}, **kw)


class _Tool:
    def __init__(self, usd=2680.0):
        self.calls = []
        self.usd = usd

    def _get_wallet(self):
        return SimpleNamespace(policy=_Gate())

    async def transfer(self, params, ctx):
        self.calls.append((params, ctx))
        if params.dry_run:
            body = (f"transfer\n  simulated value: ${self.usd:.4f}\n  guard: authorized\n"
                    "  RESULT: DRY RUN (simulation only)")
        else:
            body = "transfer\n  RESULT: SENT tx 0xabc"
        return SimpleNamespace(error=None, extracted_content=body, metadata=_facts(self.usd, params))


def test_bare_send_only_quotes_and_shows_the_caps_first():
    tool = _Tool()
    out = asyncio.run(send_ops.send_reply(
        "owner", ["0.997", "native", "to", ADDR, "on", "ethereum"], tool=tool))
    assert [p.dry_run for p, _ in tool.calls] == [True]
    assert "per transaction $3,000.00" in out
    assert "$1,000.00 used of $7,000.00" in out
    assert "/send 0.997 native to" in out and out.rstrip().endswith("go")


def test_go_quotes_then_sends_once_bounded_by_the_quote():
    tool = _Tool()
    out = asyncio.run(send_ops.send_reply(
        "owner", ["0.997", "native", "to", ADDR, "on", "ethereum", "go"], tool=tool))
    assert [p.dry_run for p, _ in tool.calls] == [True, False]
    live = tool.calls[1][0]
    assert live.max_spend_usd == pytest.approx(min(3000.0, round(2680.0 * 1.25 + 0.01, 2)))
    assert "SENT" in out


def test_the_seat_context_is_an_owner_turn_never_none():
    tool = _Tool()
    asyncio.run(send_ops.send_reply(
        "owner", ["1", "native", "to", ADDR, "on", "base", "go"], tool=tool))
    ctx = tool.calls[-1][1]
    assert ctx is not None and ctx.role == "orchestrator" and ctx.user_id == "owner"


def test_no_owner_no_send():
    assert "Only the owner" in asyncio.run(send_ops.send_reply(None, ["1"]))


def test_send_is_one_verb_on_both_seats():
    from core import money_verbs  # noqa: F401  (registers the rows)
    from core.verbs import VERB_TABLE
    rows = [v for v in VERB_TABLE if v.name == "/send"]
    assert len(rows) == 1 and rows[0].group == "money"


def test_send_go_runs_in_background_and_says_send_not_bridge(monkeypatch):
    """2026-09-26: `/send … go` answered "Bridge started" on prod."""
    from types import SimpleNamespace
    from surfaces.telegram import harness

    async def _admin(task_agent, result, cmd):
        return "sent"

    async def _deliver(text):
        pass

    monkeypatch.setattr(harness, "_handle_owner_admin", _admin)
    result = SimpleNamespace(
        decision=SimpleNamespace(command="/send", session_key="telegram:dm:1", session_id="s"),
        inbound=SimpleNamespace(text=f"/send 1 native to {ADDR} on base go",
                                identity=SimpleNamespace(user_id="alice")))
    assert harness._runs_in_background("/send", result)
    reply = asyncio.run(harness._handle_command(None, result, spawn=None, deliver=_deliver))
    assert reply.startswith("⏳ Send started") and "Bridge" not in reply


# -- validation 2026-09-27 ------------------------------------------------------

SOL_ADDR = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"


@pytest.mark.parametrize("args", [
    ["0.5", "sol", "to", ADDR, "on", "ethereum"],     # SOL named, ETH would move
    ["100", "matic", "to", ADDR, "on", "base"],       # POL/MATIC named on base
    ["1", "eth", "to", SOL_ADDR, "on", "solana"],     # ETH named, SOL would move
])
def test_another_chains_native_symbol_never_sends_this_chains_native(args):
    """`_NATIVE` mapped any gas symbol to 'native' on ANY chain: `/send 0.5 sol …
    on ethereum go` sent 0.5 ETH, and `go` sends without showing a quote first."""
    order, err = send_ops.parse(args)
    assert order is None, order
    assert "native" in err


@pytest.mark.parametrize("args,token", [
    (["1", "ETH", "to", ADDR, "on", "ethereum"], "native"),
    (["1", "native", "to", ADDR, "on", "base"], "native"),
    (["1", "sol", "to", SOL_ADDR, "on", "solana"], "native"),
])
def test_this_chains_own_native_symbol_still_sends_native(args, token):
    order, err = send_ops.parse(args)
    assert err is None and order["token"] == token


def test_each_send_is_its_own_turn_so_a_repeat_is_not_a_replay():
    """The Solana replay key hashes the turn id; the seat context had none, so the
    same `/send 1 sol to X on solana go` on any later day was 'replay blocked'."""
    from tools.defi.solana_send_verb import _idem
    a = send_ops._owner_ctx("owner")
    b = send_ops._owner_ctx("owner")
    assert _idem(None, SOL_ADDR, 10 ** 9, a) != _idem(None, SOL_ADDR, 10 ** 9, b)


def test_the_quote_speaks_to_the_owner_not_to_the_agent():
    class _AgentWords(_Tool):
        async def transfer(self, params, ctx):
            body = (f"transfer\n  simulated value: ${self.usd:.4f}\n"
                    "  RESULT: DRY RUN (simulation only — nothing was broadcast, queued "
                    "or staged) — the guard would allow this. Re-run with "
                    "dry_run=false to send.")
            return SimpleNamespace(error=None, extracted_content=body,
                                   metadata=_facts(self.usd, params))

    out = asyncio.run(send_ops.send_reply(
        "owner", ["1", "native", "to", ADDR, "on", "ethereum"], tool=_AgentWords()))
    assert "dry_run=false" not in out
    assert "/send 1 native to" in out


def test_a_disabled_wallet_says_so():
    class _NoWallet(_Tool):
        def _get_wallet(self):
            return None

    out = asyncio.run(send_ops.send_reply(
        "owner", ["1", "native", "to", ADDR, "on", "ethereum"], tool=_NoWallet()))
    assert "not enabled" in out and "Nothing was sent" in out


# -- a dollar amount the tool converts (prod 2026-10-28) ------------------------

class _PricedTool(_Tool):
    """A token at $0.0000271 (the incident's order of magnitude). The simulated
    value is what the converted amount is worth, like the real guard."""

    def __init__(self, px=0.0000271):
        super().__init__()
        self.px = px

    def _price(self, chain, addr):
        return self.px

    async def transfer(self, params, ctx):
        self.usd = params.amount * (self.px or 0)
        return await super().transfer(params, ctx)


def test_a_dollar_send_quotes_the_converted_token_count_and_the_price():
    tool = _PricedTool()
    out = asyncio.run(send_ops.send_reply(
        "owner", ["$22", TOKEN, "to", ADDR, "on", "base"], tool=tool))
    sent = tool.calls[0][0]
    assert sent.dry_run and sent.amount == pytest.approx(22 / 0.0000271, rel=1e-5)
    assert "$22.00 = 811,808" in out and "$0.00002710" in out
    # The confirm line keeps the dollars and carries the implied cap.
    assert out.rstrip().endswith(f"/send $22 {TOKEN} to {ADDR} on base max 23.11 go")
    assert "the dollars you named + 5%" in out


def test_the_confirm_line_makes_a_confirmable_quote_card(tmp_path):
    from core.surfaces import cards
    cards.set_store(cards.CardStore(str(tmp_path / "cards.db")))
    try:
        tool = _PricedTool()
        typed = ["$22", TOKEN, "to", ADDR, "on", "base"]
        out = asyncio.run(send_ops.send_reply("owner", typed, tool=tool))
        text, card = cards.quote_card("owner", "/send", typed, out)
        assert card is not None and card.args[-2:] == ["max", "23.11"]
        assert "811,808" in text                  # the owner sees the token count
    finally:
        cards.set_store(None)


def test_a_dollar_send_with_go_converts_and_is_held_to_the_dollars():
    tool = _PricedTool()
    out = asyncio.run(send_ops.send_reply(
        "owner", ["$22", TOKEN, "to", ADDR, "on", "base", "go"], tool=tool))
    assert [p.dry_run for p, _ in tool.calls] == [True, False]
    live = tool.calls[1][0]
    assert live.amount == pytest.approx(22 / 0.0000271, rel=1e-5)
    assert live.max_spend_usd == 23.11
    assert "SENT" in out


def test_a_dollar_send_without_a_spend_grade_price_is_refused_plainly():
    tool = _PricedTool(px=None)
    out = asyncio.run(send_ops.send_reply(
        "owner", ["$22", TOKEN, "to", ADDR, "on", "base", "go"], tool=tool))
    assert tool.calls == []                       # nothing quoted, nothing sent
    assert "No spend-grade price" in out and "token amount" in out


def test_a_token_send_is_unchanged_and_never_reads_a_price():
    class _NoPrice(_Tool):
        def _price(self, chain, addr):
            raise AssertionError("a token amount needs no conversion")

    tool = _NoPrice()
    out = asyncio.run(send_ops.send_reply(
        "owner", ["22", TOKEN, "to", ADDR, "on", "base"], tool=tool))
    assert tool.calls[0][0].amount == 22.0 and "amount: $" not in out


def test_the_quote_and_the_send_share_one_price_decision():
    """2026-11-18: one Confirm read each price five times and met the indexer's
    rate limit. Both guard passes run inside ONE ``one_decision`` block."""
    from tools.intel import price as P
    seen = []

    class _Watch(_Tool):
        async def transfer(self, params, ctx):
            seen.append(P._DECISION.get())
            return await super().transfer(params, ctx)

    asyncio.run(send_ops.send_reply(
        "owner", ["1", "native", "to", ADDR, "on", "base", "go"], tool=_Watch()))
    assert len(seen) == 2 and seen[0] is not None and seen[0] is seen[1]
    assert P._DECISION.get() is None


def test_a_typed_go_delivers_the_owner_view_and_a_card_deliver_gets_every_line(monkeypatch):
    """2026-11-18: the result message is the short view; a card's own deliver
    stores the whole result first (the agent reads its tx from there)."""
    from types import SimpleNamespace
    from surfaces.telegram import harness
    report = ("transfer 1 native -> x\n  simulated value: $5.0100\n"
              "  value sent: $5.0000 (without the network fee)\n  guard: authorized\n"
              "  lane:  owner_direct\n  RESULT: SENT AND CONFIRMED\n  tx: 0xabc\n  block: 7")

    async def _admin(task_agent, result, cmd):
        return report

    monkeypatch.setattr(harness, "_handle_owner_admin", _admin)
    monkeypatch.setattr("surfaces.telegram.card_ops.record_out", lambda *a, **k: None)

    def _result():
        return SimpleNamespace(
            decision=SimpleNamespace(command="/send", session_key="telegram:dm:1", session_id="s"),
            inbound=SimpleNamespace(text=f"/send 1 native to {ADDR} on base go",
                                    identity=SimpleNamespace(user_id="alice")))

    async def _go(deliver):
        reply = await harness._handle_command(None, _result(), spawn=None, deliver=deliver)
        await asyncio.gather(*list(harness._BACKGROUND_COMMANDS))
        return reply

    plain, stored = [], []

    async def _plain(text):
        plain.append(text)

    async def _card(text):
        stored.append(text)
    _card.stores_result = True

    started = asyncio.run(_go(_plain))
    asyncio.run(_go(_card))
    assert started.startswith("⏳ Send started: 1 native → ") and "on base" in started
    assert "guard:" not in plain[0] and "lane:" not in plain[0]
    assert "value: $5.00 + network fee $0.01" in plain[0] and "tx: 0xabc" in plain[0]
    assert stored == [report]
