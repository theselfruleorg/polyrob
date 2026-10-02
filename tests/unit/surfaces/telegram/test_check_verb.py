"""/check (071): any address or ticker, from the owner's seat."""
import pytest

from surfaces.telegram import check_ops

SOL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
EVM = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


class _AR:
    def __init__(self, content=None, error=None):
        self.extracted_content, self.error = content, error


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def _run(user_id, verb, params):
        seen.append((verb, params.model_dump()))
        return _AR(content=f"{verb} ok")

    monkeypatch.setattr(check_ops, "_run", _run)
    return seen


def _kind(monkeypatch, by_chain):
    import core.wallet.address_kind as ak
    monkeypatch.setattr(ak, "classify", lambda chain, addr: by_chain.get(chain))


@pytest.mark.asyncio
async def test_no_owner_no_check():
    assert "Only the owner" in await check_ops.check_reply(None, [SOL])


@pytest.mark.asyncio
async def test_bare_shows_usage():
    assert "Usage" in await check_ops.check_reply("o", [])


@pytest.mark.asyncio
async def test_a_solana_wallet_reads_its_holdings(monkeypatch, calls):
    _kind(monkeypatch, {"solana": {"kind": "wallet"}})
    out = await check_ops.check_reply("o", [SOL])
    assert out == "wallet_holdings ok"
    assert calls[0][1] == {"address": SOL, "chain": "solana"}


@pytest.mark.asyncio
async def test_a_mint_gets_the_token_report(monkeypatch, calls):
    _kind(monkeypatch, {"solana": {"kind": "token"}})
    assert await check_ops.check_reply("o", [SOL]) == "token_info ok"


@pytest.mark.asyncio
async def test_a_token_account_names_mint_and_owner(monkeypatch, calls):
    _kind(monkeypatch, {"solana": {"kind": "token_account", "mint": "M1", "owner": "W1"}})
    out = await check_ops.check_reply("o", [SOL])
    assert "/check M1" in out and "/check W1" in out and not calls


@pytest.mark.asyncio
async def test_an_unreadable_kind_still_reads_holdings(monkeypatch, calls):
    _kind(monkeypatch, {})
    assert await check_ops.check_reply("o", [SOL, "solana"]) == "wallet_holdings ok"


@pytest.mark.asyncio
async def test_a_bare_0x_wallet_shows_native_per_chain(monkeypatch, calls):
    _kind(monkeypatch, {c: {"kind": "wallet"} for c in check_ops._evm_chains()})
    import core.wallet.onchain as oc
    monkeypatch.setattr(oc, "balances",
                        lambda a, c, timeout=4.0: (None, None) if c == "polygon" else (1.5, None))
    out = await check_ops.check_reply("o", [EVM])
    assert "1.500000 ETH" in out
    assert "UNKNOWN (read failed — not zero)" in out        # polygon
    assert f"/check {EVM} <chain>" in out and not calls


@pytest.mark.asyncio
async def test_a_bare_0x_token_is_found_on_its_chain(monkeypatch, calls):
    _kind(monkeypatch, {"base": {"kind": "token"}})
    out = await check_ops.check_reply("o", [EVM])
    assert out == "token_info ok"
    assert calls[0][1]["chain"] == "base"


@pytest.mark.asyncio
async def test_unknown_chain_is_named(calls):
    out = await check_ops.check_reply("o", [EVM, "moonchain"])
    assert "moonchain" in out and "base" in out


@pytest.mark.asyncio
async def test_a_ticker_gets_candidates(calls):
    out = await check_ops.check_reply("o", ["PNL"])
    assert out == "Not an address — searched it as a ticker.\ntoken_resolve ok"
    assert calls[0][1] == {"symbol": "PNL"}


def test_owner_text_drops_operator_env_var_remedies():
    out = check_ops._owner_text(
        "unknown (rate-limited). A keyed RPC pinned in DEFI_SOLANA_RPC makes the "
        "per-address read reliable. Unknown is not wide.\n"
        "re-read for a value (or raise DEFI_PORTFOLIO_PRICE_BUDGET_SEC).\n"
        "(no ALCHEMY_API_KEY, so holdings could not be enumerated; x)")
    assert "DEFI_" not in out and "ALCHEMY_API_KEY" not in out
    assert "Unknown is not wide." in out and "re-read for a value." in out


def test_check_is_a_registered_owner_verb_on_both_seats():
    from core import verbs
    assert verbs.verb_for("/check") is not None
    assert verbs.handler_ref("telegram", "/check") == "surfaces.telegram.check_ops:check_verb"
    assert verbs.handler_ref("repl", "/check") == "cli.ui.commands.h_check:h_check"
    assert verbs.room_refused("/check")
