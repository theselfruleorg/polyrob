"""085 L3: a Solana BUY reads the mint's Token-2022 extensions on-chain and
refuses the trap shapes even when the screen (GoPlus) says the mint is clean."""
import pytest

from tools.defi import solana_swap_verb as ssv

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
DELEGATE = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"


def _account(extensions=(), program="spl-token-2022"):
    return {"value": {"data": {"program": program, "parsed": {
        "type": "mint",
        "info": {"decimals": 6, "supply": "1000", "isInitialized": True,
                 "mintAuthority": None, "freezeAuthority": None,
                 "extensions": list(extensions)}}}}}


def _with(monkeypatch, info):
    monkeypatch.setattr(ssv, "_read_mint", lambda mint: info)


@pytest.mark.parametrize("ext,word", [
    ({"extension": "permanentDelegate", "state": {"delegate": DELEGATE}}, "permanentDelegate"),
    ({"extension": "transferHook", "state": {"programId": DELEGATE}}, "transferHook"),
    ({"extension": "transferFeeConfig", "state": {
        "olderTransferFee": {"transferFeeBasisPoints": 0},
        "newerTransferFee": {"transferFeeBasisPoints": 250}}}, "transferFeeConfig"),
    ({"extension": "defaultAccountState", "state": {"accountState": "frozen"}}, "FROZEN"),
])
def test_an_active_trap_refuses(monkeypatch, ext, word):
    _with(monkeypatch, _account([ext]))
    why = ssv.mint_trap_refusal(MINT)
    assert why and word in why


@pytest.mark.parametrize("ext", [
    {"extension": "permanentDelegate", "state": {"delegate": None}},
    {"extension": "transferHook", "state": {"programId": None}},
    {"extension": "transferFeeConfig", "state": {
        "olderTransferFee": {"transferFeeBasisPoints": 0},
        "newerTransferFee": {"transferFeeBasisPoints": 0}}},
    {"extension": "defaultAccountState", "state": {"accountState": "initialized"}},
    {"extension": "tokenMetadata", "state": {"name": "T", "symbol": "T"}},
    {"extension": "interestBearingConfig", "state": {}},
])
def test_an_inactive_or_unrelated_extension_passes(monkeypatch, ext):
    _with(monkeypatch, _account([ext]))
    assert ssv.mint_trap_refusal(MINT) is None


def test_a_classic_spl_mint_passes(monkeypatch):
    _with(monkeypatch, _account(program="spl-token"))
    assert ssv.mint_trap_refusal(MINT) is None


def test_an_unreadable_mint_refuses(monkeypatch):
    def boom(mint):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(ssv, "_read_mint", boom)
    assert "could not read" in ssv.mint_trap_refusal(MINT)
    _with(monkeypatch, {"value": None})
    assert "does not read" in ssv.mint_trap_refusal(MINT)


@pytest.mark.asyncio
async def test_the_buy_refuses_a_trap_the_screen_called_clean(monkeypatch, tmp_path):
    pytest.importorskip("solders", reason="needs the `solana` extra")
    import types
    from core.wallet import token_pins
    from tools.defi.providers.base import ScreenVerdict
    from tools.defi.trade_tool import DefiTradeTool, SolanaSwapParams
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u")
    monkeypatch.setattr(token_pins, "token_pins_db_path",
                        lambda data_home=None: str(tmp_path / "pins.db"))
    _with(monkeypatch, _account([{"extension": "permanentDelegate",
                                  "state": {"delegate": DELEGATE}}]))

    class _Wallet:
        policy = types.SimpleNamespace(has_daily_cap=True)

        def solana_signer(self, account=0):
            return types.SimpleNamespace(address=DELEGATE)

    def _no_quote(*a, **k):
        raise AssertionError("the quote must not be reached")

    tool = DefiTradeTool(
        wallet=_Wallet(), solana_quote_fn=_no_quote, solana_decimals_fn=lambda m: 6,
        solana_screen_fn=lambda m: ScreenVerdict(available=True,
                                                 checks={"transfer_fee": "no"}))
    res = await tool.solana_swap(
        SolanaSwapParams(token_in="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                         token_out=MINT, amount_in=1.0, max_spend_usd=2.0,
                         dry_run=True),
        execution_context=types.SimpleNamespace(user_id="u", metadata={},
                                                role="orchestrator",
                                                is_sub_agent=False))
    assert res.error and "permanentDelegate" in res.error


@pytest.mark.parametrize("ext,word", [
    ({"extension": "nonTransferable", "state": {}}, "nonTransferable"),
    ({"extension": "pausableConfig", "state": {"authority": DELEGATE, "paused": True}}, "PAUSED"),
])
def test_an_unsellable_or_paused_mint_refuses_a_buy(monkeypatch, ext, word):
    _with(monkeypatch, _account([ext]))
    why = ssv.mint_trap_refusal(MINT)
    assert why and word in why


def test_an_unpaused_pausable_mint_passes(monkeypatch):
    _with(monkeypatch, _account([{"extension": "pausableConfig",
                                  "state": {"authority": DELEGATE, "paused": False}}]))
    assert ssv.mint_trap_refusal(MINT) is None
