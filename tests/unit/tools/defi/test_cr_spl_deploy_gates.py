"""``solana_deploy_token``: CR-M02, CR-L08, CR-L09 (crypto security analysis
2026-09-23)."""
import contextlib
from types import SimpleNamespace

import pytest

pytest.importorskip("solders")

from solders.keypair import Keypair  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from core.wallet import solana_tx_inspect as sti  # noqa: E402
from core.wallet import spl_token  # noqa: E402
from core.wallet.solana_simulation import SolanaDeltas  # noqa: E402
from tools.defi.trade_tool import DefiTradeTool, SolanaDeployTokenParams  # noqa: E402

PAYER_KP = Keypair()
PAYER = str(PAYER_KP.pubkey())
SUPPLY_RAW = 10 ** 9 * 10 ** 9


class _Gate:
    has_daily_cap = True

    def __init__(self):
        self.recorded = []

    @contextlib.asynccontextmanager
    async def reserve(self):
        yield

    def check(self, **kw):
        return SimpleNamespace(allowed=True, reason="")

    def record(self, **kw):
        self.recorded.append(kw)


class _Wallet:
    def __init__(self):
        self.policy = _Gate()

    def solana_signer(self, account=0):
        from core.wallet.solana_signer import SolanaSigner
        return SolanaSigner(PAYER_KP)


def _mint_of(raw_tx):
    return str(VersionedTransaction.from_bytes(bytes(raw_tx)).message.account_keys[1])


def _clean_sim(raw_tx, owner):
    return SolanaDeltas(ok=True, native_delta=-4_007_960,
                        token_deltas={_mint_of(raw_tx): SUPPLY_RAW})


def _params(**kw):
    base = dict(name="Rob Probe", symbol="RPROBE", supply=1_000_000_000,
                decimals=9, uri="", max_spend_usd=5.0, dry_run=False)
    base.update(kw)
    return SolanaDeployTokenParams(**base)


@pytest.fixture(autouse=True)
def _rig(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")
    monkeypatch.setenv("DEFI_DEPLOY_ENABLED", "true")
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    monkeypatch.delenv("DEFI_SOLANA_RPC", raising=False)
    monkeypatch.setattr("core.config_policy.AutonomyConfig.autonomy_halted",
                        staticmethod(lambda: False))
    monkeypatch.setattr("core.wallet.tx_guard._entry_paused", lambda: False)
    monkeypatch.setattr("core.autonomy_control.allows",
                        lambda kind: SimpleNamespace(allowed=True, reason="ok"))
    monkeypatch.setattr("core.wallet.solana_rail.SolanaRail.recent_blockhash",
                        lambda self: "11111111111111111111111111111111")
    monkeypatch.setattr(spl_token, "rent_exempt_from_rpc",
                        lambda space, rpc: 3_000_000)


def _tool(sim=_clean_sim):
    return DefiTradeTool(wallet=_Wallet(), price_fn=lambda c, a: 200.0,
                         solana_simulate_fn=sim)


# -- CR-L08 -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_failed_simulation_is_reported_not_a_traceback():
    tool = _tool(sim=lambda **k: SolanaDeltas(ok=False, reason="rpc said no"))
    res = await tool.solana_deploy_token(_params(dry_run=True))
    assert "rpc said no" in (res.error or "")


# -- CR-M02 -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_forged_turn_never_deploys(monkeypatch):
    monkeypatch.setattr(
        "tools.controller.action_registration._is_forged_or_autonomous_turn",
        lambda ctx, tool: True)
    monkeypatch.setattr("core.wallet.tx_guard._autonomous_turn_allowed",
                        lambda *a, **k: False)
    ctx = SimpleNamespace(user_id="owner", role="orchestrator",
                          is_sub_agent=False, metadata={})
    res = await _tool().solana_deploy_token(_params(), ctx)
    assert "cannot move funds" in (res.error or "")


@pytest.mark.asyncio
async def test_the_entry_pause_holds_a_deploy(monkeypatch):
    monkeypatch.setattr("core.wallet.tx_guard._entry_paused", lambda: True)
    res = await _tool().solana_deploy_token(_params())
    assert "PAUSED" in (res.error or "")


# -- CR-L09 -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_autonomous_ceiling_applies(monkeypatch):
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "0.10")
    res = await _tool().solana_deploy_token(_params(dry_run=True))
    assert "autonomous ceiling" in (res.extracted_content or res.error or "")


@pytest.mark.asyncio
async def test_an_autonomous_origin_needs_a_daily_cap(monkeypatch):
    monkeypatch.setattr(
        "tools.controller.action_registration._is_forged_or_autonomous_turn",
        lambda ctx, tool: True)
    monkeypatch.setattr("core.wallet.tx_guard._autonomous_turn_allowed",
                        lambda *a, **k: True)
    tool = _tool()
    tool._wallet.policy.has_daily_cap = False
    ctx = SimpleNamespace(user_id="owner", role="orchestrator",
                          is_sub_agent=False, metadata={})
    res = await tool.solana_deploy_token(_params(), ctx)
    assert "WALLET_DAILY_CAP_USD" in (res.extracted_content or res.error or "")


def test_the_new_mint_is_observed_even_with_many_token_accounts(monkeypatch):
    """With >= 3 held token accounts the new ATA fell past the 5-account cap
    and every deploy refused as 'supply not measured'."""
    monkeypatch.delenv("DEFI_SOLANA_SIM_MAX_ACCOUNTS", raising=False)
    tx, _kp, mint, ata = spl_token.build_fixed_supply_mint(
        payer=PAYER, decimals=9, supply_raw=SUPPLY_RAW,
        recent_blockhash="11111111111111111111111111111111",
        mint_rent=3_000_000, name="A", symbol="A", uri="")
    held = [str(Keypair().pubkey()) for _ in range(6)]

    def _acct(owner, amount):
        return {"lamports": 2_039_280, "owner": spl_token.TOKEN_2022_PROGRAM,
                "data": {"parsed": {"type": "account", "info": {
                    "owner": owner, "mint": mint, "state": "initialized",
                    "tokenAmount": {"amount": str(amount), "decimals": 9}}}}}

    def _rpc(method, params):
        if method == "getTokenAccountsByOwner":
            return {"value": [{"pubkey": p} for p in held]}
        if method == "getMultipleAccounts":
            return {"value": [{"lamports": 10 ** 9, "owner": sti.SYSTEM_PROGRAM_ID}
                              if a == PAYER else None for a in params[0]]}
        if method == "simulateTransaction":
            asked = params[1]["accounts"]["addresses"]
            assert len(asked) <= 5
            return {"value": {"err": None, "accounts": [
                {"lamports": 10 ** 9 - 5_039_280, "owner": sti.SYSTEM_PROGRAM_ID}
                if a == PAYER else _acct(PAYER, SUPPLY_RAW) if a == ata else None
                for a in asked]}}
        raise AssertionError(method)

    deltas = sti.simulate(bytes(tx), owner=PAYER, rpc=_rpc)
    assert deltas.ok, deltas.reason
    assert deltas.token_deltas.get(mint) == SUPPLY_RAW
