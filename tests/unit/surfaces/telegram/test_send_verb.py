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
    assert order == {"amount": 0.997, "token": "native", "to": ADDR,
                     "chain": "ethereum", "go": True, "max_usd": None}


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
        return SimpleNamespace(error=None, extracted_content=body)


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
    assert live.max_spend_usd == pytest.approx(round(2680.0 * 1.05 + 0.01, 2))
    assert "SENT" in out


def test_the_seat_context_is_an_owner_turn_never_none():
    tool = _Tool()
    asyncio.run(send_ops.send_reply(
        "owner", ["1", "native", "to", ADDR, "on", "base", "go"], tool=tool))
    ctx = tool.calls[-1][1]
    assert ctx is not None and ctx.role == "owner" and ctx.user_id == "owner"


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
                                identity=SimpleNamespace(user_id="rob")))
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
            return SimpleNamespace(error=None, extracted_content=body)

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
