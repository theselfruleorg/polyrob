"""``defi_trade.solana_transfer`` — send SOL or an SPL token, guarded.

The Solana twin of ``defi_trade.transfer``'s contract: dry_run defaults TRUE,
``max_spend_usd`` is asserted against the SIMULATED outflow, the guard verdict
and the lane are shown, a refusal says "NOT SENT — nothing was broadcast", the
owner-queue lane does not execute, the ledger records on broadcast (even on a
failed landing) and the receipt states are honest. Nearly every test here is a
refusal.
"""
import types

import pytest

solders = pytest.importorskip("solders", reason="needs the `solana` extra")

from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from core.wallet import spl_token as S  # noqa: E402
from core.wallet.solana_simulation import SolanaDeltas  # noqa: E402
from tools.defi import solana_send_verb as V  # noqa: E402
from tools.defi.trade_tool import DefiTradeTool  # noqa: E402

ME = str(Keypair().pubkey())
TO = str(Keypair().pubkey())
MINT = str(Keypair().pubkey())
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
FEE = 5_000
ATA_RENT = 2_039_280
SOL_PX = 150.0


class _Gate:
    has_daily_cap = True

    def __init__(self, allow=True):
        self.recorded, self.checked = [], []
        self._allow = allow

    def check(self, *, venue, amount_usd, idempotency_key):
        self.checked.append((venue, amount_usd, idempotency_key))
        return types.SimpleNamespace(allowed=self._allow, reason="daily cap reached")

    def record(self, **kw):
        self.recorded.append(kw)

    def reserve(self):
        class _C:
            async def __aenter__(s): return None
            async def __aexit__(s, *a): return False
        return _C()


class _Signer:
    address = ME


class _Wallet:
    def __init__(self, gate=None):
        self.policy = gate or _Gate()

    def solana_signer(self, account=0):
        return _Signer()


def _mint_info(*, program="spl-token", decimals=6, extensions=None, type_="mint"):
    info = {"decimals": decimals, "supply": "1000000000", "mintAuthority": None,
            "freezeAuthority": None, "isInitialized": True}
    if extensions is not None:
        info["extensions"] = extensions
    return {"value": {"owner": (S.TOKEN_2022_PROGRAM if program == "spl-token-2022"
                                else S.TOKEN_PROGRAM),
                      "data": {"program": program,
                               "parsed": {"type": type_, "info": info}}}}


class _Rpc:
    """A fake RPC: the recipient, the mint and our native balance."""

    def __init__(self, *, recipient=None, mint=None, balance=10 ** 10, held=10 ** 12):
        self.recipient = recipient if recipient is not None else {"value": None}
        self.mint = mint if mint is not None else _mint_info()
        self.balance = balance
        self.held = held
        self.calls = []
        self._mint_addr = None

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "getBalance":
            if isinstance(self.balance, Exception):
                raise self.balance
            return {"value": self.balance}
        if method == "getAccountInfo":
            target = params[0]
            if target == TO:
                if isinstance(self.recipient, Exception):
                    raise self.recipient
                return self.recipient
            if self._mint_addr is None:
                self._mint_addr = target
                return self.mint
            # Our source associated token account.
            if isinstance(self.held, Exception):
                raise self.held
            if self.held is None:
                return {"value": None}
            return {"value": {"owner": S.TOKEN_PROGRAM, "data": {"parsed": {"info": {
                "mint": self._mint_addr, "owner": ME, "state": "initialized",
                "tokenAmount": {"amount": str(self.held)}}}}}}
        raise AssertionError(f"unexpected RPC {method}")


def _native_deltas(amount_raw, **kw):
    base = dict(ok=True, native_delta=-(amount_raw + FEE), token_deltas={},
                fee_lamports=FEE, retained_rent_lamports=0,
                native_excess=amount_raw)
    base.update(kw)
    return SolanaDeltas(**base)


def _spl_deltas(amount_raw, *, mint=MINT, rent=ATA_RENT, **kw):
    base = dict(ok=True, native_delta=-(FEE + rent),
                token_deltas={mint: -amount_raw}, fee_lamports=FEE,
                retained_rent_lamports=0, native_excess=rent,
                authority_grants=((("owner_changed", mint, TO),) if rent else ()))
    base.update(kw)
    return SolanaDeltas(**base)


def _price(chain, addr):
    return {WSOL: SOL_PX, MINT: 2.0}.get(addr)


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    monkeypatch.setenv("DEFI_SOLANA_RPC", "https://pinned.example")
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "100")
    monkeypatch.delenv("DEFI_AUTONOMOUS_TURN_TRADING", raising=False)
    monkeypatch.setattr(V, "_recent_blockhash", lambda: str(Hash.default()))


def _tool(monkeypatch, *, deltas, rpc=None, wallet=None, held=10 ** 12,
          sent=None, confirm=(True, "confirmed"), send_exc=None, **kw):
    rpc = rpc or _Rpc(held=held)
    monkeypatch.setattr(V, "_rpc", rpc)
    seen = {}

    def _simulate(**k):
        seen["raw_tx"] = k["raw_tx"]
        return deltas

    def _send(raw):
        if send_exc:
            raise send_exc
        if sent is not None:
            sent.append(raw)
        return "SIG111"

    tool = DefiTradeTool(
        wallet=wallet or _Wallet(), price_fn=_price,
        solana_simulate_fn=_simulate, solana_send_fn=_send,
        solana_confirm_fn=lambda sig: confirm,
        solana_blockhash_fn=lambda raw: (True, "valid"), **kw)
    tool._seen = seen
    return tool


def _params(**kw):
    base = dict(token="native", to=TO, amount=0.5, max_spend_usd=100.0)
    base.update(kw)
    return V.SolanaTransferParams(**base)


def _text(res):
    return (res.extracted_content or "") + (res.error or "")


# ==========================================================================
# Registration + classification
# ==========================================================================

def test_verb_is_registered_on_the_tool_and_classified():
    import inspect
    member = inspect.getattr_static(DefiTradeTool, "solana_transfer")
    assert callable(member)
    from core.verb_policy import ids_where
    assert "defi_trade_solana_transfer" in ids_where(lane="owner_always")
    assert "defi_trade_solana_transfer" in ids_where(simulatable=True)


def test_dry_run_defaults_true():
    assert V.SolanaTransferParams(token="native", to=TO, amount=1,
                                  max_spend_usd=1).dry_run is True


# ==========================================================================
# Native SOL
# ==========================================================================

@pytest.mark.asyncio
async def test_native_dry_run_shows_verdict_and_lane_and_sends_nothing(monkeypatch):
    sent, wallet = [], _Wallet()
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000), sent=sent,
                 wallet=wallet)
    res = await tool.solana_transfer(_params())
    out = _text(res)
    assert res.error is None, out
    assert "guard:" in out and "lane:  autonomous" in out
    assert "DRY RUN" in out and "nothing was broadcast" in out
    assert "simulated value: $75.0100" in out
    assert sent == [] and wallet.policy.recorded == []
    # The bytes simulated ARE a System transfer of exactly the amount to TO.
    tx = VersionedTransaction.from_bytes(tool._seen["raw_tx"])
    keys = [str(k) for k in tx.message.account_keys]
    (ix,) = tx.message.instructions
    assert keys[ix.program_id_index] == S.SYSTEM_PROGRAM
    assert keys[bytes(ix.accounts)[1]] == TO
    assert int.from_bytes(bytes(ix.data)[4:12], "little") == 500_000_000


@pytest.mark.asyncio
async def test_native_live_send_records_and_confirms(monkeypatch):
    sent, wallet = [], _Wallet()
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000), sent=sent,
                 wallet=wallet)
    res = await tool.solana_transfer(_params(dry_run=False))
    out = _text(res)
    assert "RESULT: SENT AND CONFIRMED" in out, out
    assert "SIG111" in out
    assert len(sent) == 1
    (rec,) = wallet.policy.recorded
    assert rec["action"] == "solana_transfer" and rec["chain"] == "solana"
    assert rec["counterparty"] == TO and rec["result_ref"] == "SIG111"
    assert rec["amount_usd"] == 75.01


@pytest.mark.asyncio
async def test_simulated_principal_must_equal_the_declared_amount(monkeypatch):
    sent = []
    tool = _tool(monkeypatch, sent=sent, deltas=_native_deltas(
        500_000_000, native_delta=-(900_000_000 + FEE), native_excess=900_000_000))
    res = await tool.solana_transfer(_params(dry_run=False))
    assert "NOT SENT" in _text(res) and sent == []


@pytest.mark.asyncio
async def test_native_send_that_moves_a_token_refuses(monkeypatch):
    tool = _tool(monkeypatch, deltas=_native_deltas(
        500_000_000, token_deltas={USDC: -5}))
    res = await tool.solana_transfer(_params())
    assert "NOT SENT" in _text(res)


@pytest.mark.asyncio
async def test_max_spend_usd_is_asserted_against_the_simulation(monkeypatch):
    sent, wallet = [], _Wallet()
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000), sent=sent,
                 wallet=wallet)
    res = await tool.solana_transfer(_params(max_spend_usd=10.0, dry_run=False))
    out = _text(res)
    assert "max_spend_usd" in out and "NOT SENT" in out
    assert sent == [] and wallet.policy.recorded == []


@pytest.mark.asyncio
async def test_above_the_autonomous_ceiling_is_owner_queue_and_not_sent(monkeypatch):
    monkeypatch.setattr("core.wallet.tx_guard.autonomous_max_usd", lambda *a, **k: 10.0)
    sent = []
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000), sent=sent)
    res = await tool.solana_transfer(_params(dry_run=False))
    out = _text(res)
    assert "owner_queue" in out and "NOT SENT" in out and sent == []


@pytest.mark.asyncio
async def test_policygate_refusal_is_not_sent(monkeypatch):
    sent, wallet = [], _Wallet(_Gate(allow=False))
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000), sent=sent,
                 wallet=wallet)
    res = await tool.solana_transfer(_params(dry_run=False))
    out = _text(res)
    assert "PolicyGate" in out and "NOT SENT" in out and sent == []


@pytest.mark.asyncio
async def test_turn_gate_refusal_is_not_sent(monkeypatch):
    sent = []
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000), sent=sent)
    tool._solana_turn_gate = lambda ctx, *, exit_shaped_fn: (
        "refused: a forged/autonomous turn cannot move funds", False, False)
    res = await tool.solana_transfer(_params(dry_run=False))
    assert "forged" in _text(res) and sent == []


@pytest.mark.asyncio
async def test_the_rail_flag_off_refuses(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "false")
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000))
    res = await tool.solana_transfer(_params())
    assert "SOLANA_TRADE_ENABLED" in _text(res)


@pytest.mark.asyncio
async def test_unpinned_rpc_refuses_a_live_send_but_not_a_dry_run(monkeypatch):
    monkeypatch.delenv("DEFI_SOLANA_RPC", raising=False)
    sent = []
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000), sent=sent)
    assert "DRY RUN" in _text(await tool.solana_transfer(_params()))
    res = await tool.solana_transfer(_params(dry_run=False))
    assert "DEFI_SOLANA_RPC" in _text(res) and sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["0x" + "ab" * 20, "not-base58-0OIl", "abc"])
async def test_non_base58_recipient_refuses(monkeypatch, bad):
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000))
    res = await tool.solana_transfer(_params(to=bad))
    assert res.error and "NOT SENT" in res.error


@pytest.mark.asyncio
async def test_sending_to_ourselves_refuses(monkeypatch):
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000))
    res = await tool.solana_transfer(_params(to=ME))
    assert "yourself" in _text(res) or "own address" in _text(res)


@pytest.mark.asyncio
async def test_a_token_account_recipient_refuses(monkeypatch):
    rpc = _Rpc(recipient={"value": {"owner": S.TOKEN_PROGRAM, "executable": False}})
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000), rpc=rpc)
    res = await tool.solana_transfer(_params())
    assert "token account" in _text(res) and "NOT SENT" in _text(res)


@pytest.mark.asyncio
async def test_an_unreadable_recipient_refuses(monkeypatch):
    rpc = _Rpc(recipient=RuntimeError("rpc down"))
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000), rpc=rpc)
    res = await tool.solana_transfer(_params())
    assert "NOT SENT" in _text(res)


@pytest.mark.asyncio
async def test_native_balance_short_or_unreadable_refuses(monkeypatch):
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000),
                 rpc=_Rpc(balance=1_000))
    assert "holds" in _text(await tool.solana_transfer(_params()))
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000),
                 rpc=_Rpc(balance=RuntimeError("down")))
    assert "NOT SENT" in _text(await tool.solana_transfer(_params()))


@pytest.mark.asyncio
async def test_excess_precision_refuses(monkeypatch):
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000))
    res = await tool.solana_transfer(_params(amount=0.0000000001))
    assert "decimals" in _text(res) and "NOT SENT" in _text(res)


@pytest.mark.asyncio
async def test_unpriceable_sol_refuses(monkeypatch):
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000))
    tool._price_fn = lambda c, a: None
    res = await tool.solana_transfer(_params())
    assert "priced" in _text(res) and "NOT SENT" in _text(res)


# ==========================================================================
# Receipt honesty
# ==========================================================================

@pytest.mark.asyncio
async def test_a_failed_landing_is_recorded_and_says_failed(monkeypatch):
    wallet = _Wallet()
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000), wallet=wallet,
                 confirm=(False, "landed but FAILED on-chain: err=X. The fee was still paid."))
    res = await tool.solana_transfer(_params(dry_run=False))
    out = _text(res)
    assert "FAILED ON-CHAIN" in out and "did NOT happen" in out
    assert len(wallet.policy.recorded) == 1


@pytest.mark.asyncio
async def test_an_unconfirmed_send_says_do_not_retry(monkeypatch):
    wallet = _Wallet()
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000), wallet=wallet,
                 confirm=(False, "UNKNOWN — the cluster has not seen this signature."))
    res = await tool.solana_transfer(_params(dry_run=False))
    out = _text(res)
    assert "NOT CONFIRMED" in out and "do NOT retry" in out
    assert len(wallet.policy.recorded) == 1


@pytest.mark.asyncio
async def test_a_broadcast_error_records_nothing(monkeypatch):
    wallet = _Wallet()
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000), wallet=wallet,
                 send_exc=RuntimeError("node said no"))
    res = await tool.solana_transfer(_params(dry_run=False))
    assert "broadcast failed" in _text(res) and wallet.policy.recorded == []


@pytest.mark.asyncio
async def test_a_lost_rpc_reply_says_outcome_unknown_not_not_sent(monkeypatch):
    """The RPC may have accepted the bytes: the owner must not be told NOT sent."""
    tool = _tool(monkeypatch, deltas=_native_deltas(500_000_000),
                 send_exc=RuntimeError("submission outcome unknown for 5sig; "
                                       "reconcile before retrying"))
    res = await tool.solana_transfer(_params(dry_run=False))
    text = _text(res)
    assert "outcome unknown" in text and "5sig" in text
    assert "NOT sent" not in text and "do not send again" in text


# ==========================================================================
# SPL tokens
# ==========================================================================

def _spl(**kw):
    return _params(token=MINT, amount=10.0, **kw)


@pytest.mark.asyncio
async def test_spl_dry_run_charges_the_recipient_account_rent(monkeypatch):
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000))
    res = await tool.solana_transfer(_spl())
    out = _text(res)
    assert res.error is None, out
    assert "DRY RUN" in out
    assert "creates the recipient's token account" in out
    # $20 of token + (fee + rent) SOL at $150.
    expected = 20.31  # Round principal plus fee and rent upward to a cent.
    assert f"simulated value: ${expected:.4f}" in out
    tx = VersionedTransaction.from_bytes(tool._seen["raw_tx"])
    keys = [str(k) for k in tx.message.account_keys]
    create, xfer = tx.message.instructions
    assert keys[create.program_id_index] == S.ATA_PROGRAM
    assert bytes(xfer.data) == bytes([12]) + (10_000_000).to_bytes(8, "little") + bytes([6])


@pytest.mark.asyncio
async def test_spl_live_send_records(monkeypatch):
    wallet, sent = _Wallet(), []
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000), wallet=wallet, sent=sent)
    res = await tool.solana_transfer(_spl(dry_run=False))
    assert "SENT AND CONFIRMED" in _text(res), _text(res)
    assert len(sent) == 1 and wallet.policy.recorded[0]["action"] == "solana_transfer"


@pytest.mark.asyncio
async def test_spl_to_an_existing_account_has_no_rent(monkeypatch):
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000, rent=0))
    out = _text(await tool.solana_transfer(_spl()))
    assert "DRY RUN" in out and "already exists" in out


@pytest.mark.asyncio
async def test_spl_unknown_decimals_refuses(monkeypatch):
    rpc = _Rpc(mint={"value": None})
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000), rpc=rpc)
    res = await tool.solana_transfer(_spl())
    assert "decimals" in _text(res) and "NOT SENT" in _text(res)


@pytest.mark.asyncio
async def test_spl_token_account_given_as_mint_refuses(monkeypatch):
    rpc = _Rpc(mint=_mint_info(type_="account"))
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000), rpc=rpc)
    assert "NOT SENT" in _text(await tool.solana_transfer(_spl()))


@pytest.mark.asyncio
async def test_spl_not_enough_held_refuses(monkeypatch):
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000), held=5)
    out = _text(await tool.solana_transfer(_spl()))
    assert "holds" in out and "NOT SENT" in out


@pytest.mark.asyncio
async def test_spl_unknown_holding_refuses(monkeypatch):
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000),
                 held=RuntimeError("rpc down"))
    assert "NOT SENT" in _text(await tool.solana_transfer(_spl()))


@pytest.mark.asyncio
async def test_spl_no_source_account_means_nothing_held(monkeypatch):
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000), held=None)
    out = _text(await tool.solana_transfer(_spl()))
    assert "holds 0" in out and "NOT SENT" in out


@pytest.mark.asyncio
async def test_token_2022_holding_is_read_from_the_2022_account(monkeypatch):
    rpc = _Rpc(mint=_mint_info(program="spl-token-2022"), held=10 ** 12)
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000), rpc=rpc)
    out = _text(await tool.solana_transfer(_spl()))
    assert "DRY RUN" in out, out
    src = S.associated_token_address(ME, MINT, S.TOKEN_2022_PROGRAM)
    assert any(m == "getAccountInfo" and p[0] == src for m, p in rpc.calls)


@pytest.mark.asyncio
async def test_wrapped_sol_mint_points_at_native(monkeypatch):
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000))
    out = _text(await tool.solana_transfer(_params(token=WSOL)))
    assert "native" in out and "NOT SENT" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", [
    {"extension": "transferFeeConfig", "state": {
        "olderTransferFee": {"transferFeeBasisPoints": 0},
        "newerTransferFee": {"transferFeeBasisPoints": 100}}},
    {"extension": "transferHook", "state": {"programId": str(Keypair().pubkey())}},
    {"extension": "nonTransferable", "state": {}},
    {"extension": "somethingNew", "state": {}},
    {"extension": "defaultAccountState", "state": {"accountState": "frozen"}},
])
async def test_token_2022_extensions_that_cannot_be_asserted_refuse(monkeypatch, ext):
    rpc = _Rpc(mint=_mint_info(program="spl-token-2022", extensions=[ext]))
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000), rpc=rpc)
    out = _text(await tool.solana_transfer(_spl()))
    assert "NOT SENT" in out and ext["extension"] in out


@pytest.mark.asyncio
async def test_token_2022_benign_extensions_pass(monkeypatch):
    rpc = _Rpc(mint=_mint_info(program="spl-token-2022", extensions=[
        {"extension": "metadataPointer", "state": {}},
        {"extension": "tokenMetadata", "state": {}},
        {"extension": "transferFeeConfig", "state": {
            "olderTransferFee": {"transferFeeBasisPoints": 0},
            "newerTransferFee": {"transferFeeBasisPoints": 0}}},
        {"extension": "transferHook", "state": {"programId": None}},
    ]))
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000), rpc=rpc)
    out = _text(await tool.solana_transfer(_spl()))
    assert "DRY RUN" in out, out
    tx = VersionedTransaction.from_bytes(tool._seen["raw_tx"])
    keys = [str(k) for k in tx.message.account_keys]
    assert keys[tx.message.instructions[1].program_id_index] == S.TOKEN_2022_PROGRAM


@pytest.mark.asyncio
@pytest.mark.parametrize("deltas", [
    _spl_deltas(10_000_001),                                    # wrong amount
    _spl_deltas(10_000_000, token_deltas={}),                   # unobserved
    _spl_deltas(10_000_000, token_deltas={MINT: -10_000_000, USDC: -1}),  # extra
    _spl_deltas(10_000_000, authority_grants=(("delegate", MINT, TO),)),
    _spl_deltas(10_000_000, rent=9_000_000),                    # rent too large
    _spl_deltas(10_000_000, fee_lamports=None),                 # fee unknown
    SolanaDeltas(ok=False, reason="simulation reverted"),
])
async def test_spl_simulation_mismatches_refuse(monkeypatch, deltas):
    sent = []
    tool = _tool(monkeypatch, deltas=deltas, sent=sent)
    res = await tool.solana_transfer(_spl(dry_run=False))
    assert "NOT SENT" in _text(res) and sent == []


@pytest.mark.asyncio
async def test_spl_unpriceable_token_refuses(monkeypatch):
    tool = _tool(monkeypatch, deltas=_spl_deltas(10_000_000))
    tool._price_fn = lambda c, a: SOL_PX if a == WSOL else None
    out = _text(await tool.solana_transfer(_spl()))
    assert "priced" in out and "NOT SENT" in out


@pytest.mark.asyncio
async def test_usdc_values_at_one_dollar(monkeypatch):
    rpc = _Rpc(mint=_mint_info(decimals=6))
    tool = _tool(monkeypatch, rpc=rpc, deltas=_spl_deltas(
        10_000_000, mint=USDC, rent=0))
    tool._price_fn = lambda c, a: SOL_PX if a == WSOL else None
    out = _text(await tool.solana_transfer(_params(token=USDC, amount=10.0)))
    assert "DRY RUN" in out, out
    assert "simulated value: $10.0100" in out


# ==========================================================================
# Through the REAL vetted simulation (solana_tx_inspect.simulate + parse_deltas)
# ==========================================================================

def _token_acct(owner, amount, lamports=ATA_RENT):
    return {"lamports": lamports, "owner": S.TOKEN_PROGRAM, "data": {
        "program": "spl-token", "parsed": {"type": "account", "info": {
            "mint": MINT, "owner": owner, "state": "initialized",
            "tokenAmount": {"amount": str(amount), "decimals": 6}}}}}


class _SimRpc:
    """Serves the observation plan + simulateTransaction for one SPL send that
    creates the recipient's associated account."""

    def __init__(self, *, recipient_post_owner=TO, sent=10_000_000):
        self.our_ata = S.associated_token_address(ME, MINT, S.TOKEN_PROGRAM)
        self.their_ata = S.associated_token_address(TO, MINT, S.TOKEN_PROGRAM)
        self.recipient_post_owner = recipient_post_owner
        self.sent = sent

    def __call__(self, method, params, *a, **k):
        if method == "getAccountInfo":
            return {"value": {"owner": S.TOKEN_PROGRAM}}
        if method == "getTokenAccountsByOwner":
            prog = params[1]["programId"]
            return {"value": ([{"pubkey": self.our_ata}]
                              if prog == S.TOKEN_PROGRAM else [])}
        if method == "getMultipleAccounts":
            return {"value": [self._pre(a) for a in params[0]]}
        if method == "simulateTransaction":
            addrs = params[1]["accounts"]["addresses"]
            return {"value": {"err": None, "unitsConsumed": 9000,
                              "accounts": [self._post(a) for a in addrs]}}
        raise AssertionError(method)

    def _pre(self, addr):
        if addr == ME:
            return {"lamports": 10 ** 10, "owner": S.SYSTEM_PROGRAM,
                    "data": ["", "base64"]}
        if addr == self.our_ata:
            return _token_acct(ME, 100_000_000)
        return None

    def _post(self, addr):
        if addr == ME:
            return {"lamports": 10 ** 10 - FEE - ATA_RENT,
                    "owner": S.SYSTEM_PROGRAM, "data": ["", "base64"]}
        if addr == self.our_ata:
            return _token_acct(ME, 100_000_000 - self.sent)
        if addr == self.their_ata:
            return _token_acct(self.recipient_post_owner, self.sent)
        return None


def _real_sim_tool(monkeypatch, sim_rpc, sent):
    monkeypatch.setattr(V, "_rpc", _Rpc())
    monkeypatch.setattr("core.wallet.solana_rail.SolanaRail._rpc",
                        lambda self, m, p, *a, **k: sim_rpc(m, p))

    def _send(raw):
        sent.append(raw)
        return "SIG111"

    return DefiTradeTool(
        wallet=_Wallet(), price_fn=_price, solana_send_fn=_send,
        solana_confirm_fn=lambda sig: (True, "confirmed"),
        solana_blockhash_fn=lambda raw: (True, "valid"))


@pytest.mark.asyncio
async def test_real_simulation_accepts_creating_the_recipients_account(monkeypatch):
    sent = []
    tool = _real_sim_tool(monkeypatch, _SimRpc(), sent)
    res = await tool.solana_transfer(_spl(dry_run=False))
    out = _text(res)
    assert "SENT AND CONFIRMED" in out, out
    assert f"{ATA_RENT} rent" in out and len(sent) == 1


@pytest.mark.asyncio
async def test_real_simulation_refuses_a_created_account_owned_by_a_stranger(monkeypatch):
    sent = []
    stranger = str(Keypair().pubkey())
    tool = _real_sim_tool(monkeypatch, _SimRpc(recipient_post_owner=stranger), sent)
    out = _text(await tool.solana_transfer(_spl(dry_run=False)))
    assert "NOT SENT" in out and sent == []


@pytest.mark.asyncio
async def test_real_simulation_refuses_a_short_debit(monkeypatch):
    sent = []
    tool = _real_sim_tool(monkeypatch, _SimRpc(sent=9_999_999), sent)
    out = _text(await tool.solana_transfer(_spl(dry_run=False)))
    assert "NOT SENT" in out and sent == []


@pytest.mark.asyncio
async def test_transfer_native_and_token_price_reads_run_off_loop(monkeypatch):
    import threading
    loop_thread = threading.get_ident()
    seen = []
    tool = _tool(monkeypatch, deltas=_spl_deltas(5_000_000))
    def price(chain, mint):
        seen.append((mint, threading.get_ident()))
        return _price(chain, mint)
    tool._price_fn = price
    res = await tool.solana_transfer(_params(token=MINT, amount=5))
    assert not res.error, _text(res)
    assert {mint for mint, _ in seen} == {WSOL, MINT}
    assert all(thread != loop_thread for _, thread in seen)
