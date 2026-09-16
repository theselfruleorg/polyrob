"""``solana_deploy_token`` — the gates and the assertions (042b).

The proof shape differs from the EVM twin because the chain does: a mint has no
bytecode, so what is asserted is STATE — the simulated supply arriving, and the
revocation read back afterwards. Nearly every test here is a refusal.
"""
import contextlib

import pytest

solders = pytest.importorskip("solders")

from core.wallet import spl_token as S  # noqa: E402
from core.wallet.solana_simulation import SolanaDeltas  # noqa: E402
from tools.defi import spl_deploy_verb as V  # noqa: E402
from tools.defi.trade_tool import DefiTradeTool, SolanaDeployTokenParams  # noqa: E402

from solders.keypair import Keypair  # noqa: E402

PAYER_KP = Keypair()
PAYER = str(PAYER_KP.pubkey())
MINT = str(Keypair().pubkey())
SUPPLY_RAW = 10 ** 9 * 10 ** 9


class _Gate:
    def __init__(self, allow=True):
        self.recorded = []
        self._allow = allow

    @contextlib.asynccontextmanager
    async def reserve(self):
        yield

    def check(self, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(allowed=self._allow, reason="test")

    def record(self, **kw):
        self.recorded.append(kw)


class _Wallet:
    def __init__(self, gate):
        self.policy = gate

    def solana_signer(self, account=0):
        from core.wallet.solana_signer import SolanaSigner
        return SolanaSigner(PAYER_KP)


def _deltas(**kw):
    base = dict(ok=True, native_delta=-3_510_880,
                token_deltas={MINT: SUPPLY_RAW}, authority_grants=())
    base.update(kw)
    return SolanaDeltas(**base)


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("DEFI_DEPLOY_ENABLED", "true")
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")


class _Leaf:
    role = "leaf"
    is_sub_agent = False
    user_id = "owner"
    metadata = {}


def _params(**kw):
    base = dict(name="Rob Probe", symbol="RPROBE", supply=1_000_000_000,
                decimals=9, uri="https://example.org/r.json",
                max_spend_usd=5.0, dry_run=True)
    base.update(kw)
    return SolanaDeployTokenParams(**base)


# ==========================================================================
# Gates
# ==========================================================================

@pytest.mark.asyncio
async def test_it_is_off_by_default(monkeypatch):
    monkeypatch.delenv("DEFI_DEPLOY_ENABLED", raising=False)
    tool = DefiTradeTool(wallet=_Wallet(_Gate()))
    res = await tool.solana_deploy_token(_params())
    assert "DEFI_DEPLOY_ENABLED" in (res.error or "")


@pytest.mark.asyncio
async def test_the_solana_rail_must_be_armed_too(monkeypatch):
    """Two keys: deployment is a capability, and Solana money is a separate
    one the operator arms on its own."""
    monkeypatch.delenv("SOLANA_TRADE_ENABLED", raising=False)
    tool = DefiTradeTool(wallet=_Wallet(_Gate()))
    res = await tool.solana_deploy_token(_params())
    assert "SOLANA_TRADE_ENABLED" in (res.error or "")


@pytest.mark.asyncio
async def test_a_leaf_never_deploys():
    tool = DefiTradeTool(wallet=_Wallet(_Gate()))
    res = await tool.solana_deploy_token(_params(), _Leaf())
    assert "sub-agent" in (res.error or "")


# ==========================================================================
# Supply conversion
# ==========================================================================

def test_the_supply_is_exact_not_float_scaled():
    """Same lesson as the EVM template: 1e9 * 10**9 in binary floating point is
    not 1e18, and above 2^53 the error is in the integer part."""
    assert V._supply_to_raw(1_000_000_000.0, 9) == 10 ** 18
    assert V._supply_to_raw(1_000_000_000, 9) == 10 ** 18


def test_a_supply_that_overflows_a_u64_refuses_with_the_remedy():
    with pytest.raises(ValueError) as exc:
        V._supply_to_raw(10 ** 12, 9)          # 1e21 raw, over u64
    assert "u64" in str(exc.value)
    assert "fewer decimals" in str(exc.value)


def test_a_fractional_supply_at_zero_decimals_refuses():
    with pytest.raises(ValueError):
        V._supply_to_raw(1.5, 0)


# ==========================================================================
# Transaction shape
# ==========================================================================

def test_a_transaction_with_the_wrong_signers_is_refused():
    tx, _kp, mint, _ata = S.build_fixed_supply_mint(
        payer=PAYER, decimals=9, supply_raw=SUPPLY_RAW,
        recent_blockhash=str(solders.hash.Hash.default()),
        mint_rent=S.rent_exempt_lamports(S.MINT_WITH_POINTER_LEN),
        name="Rob Probe", symbol="RPROBE")
    assert V._refuse_shape(tx, payer=PAYER, mint=mint) is None
    assert "not this wallet" in V._refuse_shape(tx, payer=MINT, mint=mint)
    assert "not the mint account" in V._refuse_shape(tx, payer=PAYER, mint=PAYER)


# ==========================================================================
# Delta assertions
# ==========================================================================

def test_a_clean_simulation_passes():
    assert V._assert_deltas(_deltas(), mint=MINT, supply_raw=SUPPLY_RAW) is None


def test_an_authority_grant_refuses():
    why = V._assert_deltas(
        _deltas(authority_grants=(("delegate", MINT, "someone"),)),
        mint=MINT, supply_raw=SUPPLY_RAW)
    assert why and "grants an authority" in why


def test_SOL_arriving_refuses():
    why = V._assert_deltas(_deltas(native_delta=5_000),
                           mint=MINT, supply_raw=SUPPLY_RAW)
    assert why and "SOL arriving" in why


def test_zero_SOL_movement_is_a_measurement_failure_not_a_free_mint():
    why = V._assert_deltas(_deltas(native_delta=0),
                           mint=MINT, supply_raw=SUPPLY_RAW)
    assert why and "measurement failure" in why


def test_an_outflow_beyond_two_accounts_of_rent_refuses():
    why = V._assert_deltas(_deltas(native_delta=-9_000_000),
                           mint=MINT, supply_raw=SUPPLY_RAW)
    assert why and "above the" in why


def test_an_unmeasured_supply_refuses_rather_than_assuming_it_worked():
    why = V._assert_deltas(_deltas(token_deltas={}),
                           mint=MINT, supply_raw=SUPPLY_RAW)
    assert why and "measurement failure" in why


def test_a_short_mint_refuses():
    why = V._assert_deltas(_deltas(token_deltas={MINT: SUPPLY_RAW - 1}),
                           mint=MINT, supply_raw=SUPPLY_RAW)
    assert why and "not the declared" in why


# ==========================================================================
# The read-back — the ONLY place the revocation can be seen
# ==========================================================================

def _info(*, extensions="default", **over):
    if extensions == "default":
        extensions = [{"extension": "tokenMetadata",
                       "state": {"name": "Rob Probe", "symbol": "RPROBE",
                                 "uri": "https://example.org/r.json",
                                 "updateAuthority": None,
                                 "additionalMetadata": []}}]
    fields = {"decimals": 9, "supply": str(SUPPLY_RAW),
              "mintAuthority": None, "freezeAuthority": None,
              "isInitialized": True, "extensions": extensions}
    fields.update(over)
    return {"value": {"data": {"program": "spl-token-2022",
                               "parsed": {"info": fields}}}}


def test_a_clean_read_back_states_what_is_true(monkeypatch):
    monkeypatch.setattr("core.wallet.solana_onchain._rpc",
                        lambda m, p: _info())
    text = V._prove_fixed_supply(MINT, SUPPLY_RAW, 9)
    assert "authorities all null" in text
    assert "read back from the mint, not assumed" in text


def test_a_SURVIVING_mint_authority_is_called_out_loudly(monkeypatch):
    """The delta parser is structurally blind to SetAuthority on a MINT, so if
    the revocation silently failed this read is the only thing that notices."""
    monkeypatch.setattr("core.wallet.solana_onchain._rpc",
                        lambda m, p: _info(mintAuthority=PAYER))
    text = V._prove_fixed_supply(MINT, SUPPLY_RAW, 9)
    assert "VERIFIED AND WRONG" in text
    assert "STILL" in text
    assert "Do NOT describe this token as fixed-supply" in text


def test_a_surviving_freeze_authority_is_called_out(monkeypatch):
    monkeypatch.setattr("core.wallet.solana_onchain._rpc",
                        lambda m, p: _info(freezeAuthority=PAYER))
    assert "VERIFIED AND WRONG" in V._prove_fixed_supply(MINT, SUPPLY_RAW, 9)


def test_a_wrong_supply_is_called_out(monkeypatch):
    monkeypatch.setattr("core.wallet.solana_onchain._rpc",
                        lambda m, p: _info(supply="1"))
    assert "VERIFIED AND WRONG" in V._prove_fixed_supply(MINT, SUPPLY_RAW, 9)


def test_an_unreadable_mint_is_UNVERIFIED_never_fixed_supply(monkeypatch):
    """Unknown is not 'fine'. An unreadable mint must never render as a
    successful revocation."""
    def _boom(m, p):
        raise RuntimeError("rpc down")
    monkeypatch.setattr("core.wallet.solana_onchain._rpc", _boom)
    text = V._prove_fixed_supply(MINT, SUPPLY_RAW, 9)
    assert "VERIFICATION UNAVAILABLE" in text
    assert "unverified" in text
    assert "authorities all null" not in text


def test_the_pinned_RPC_check_uses_the_SAME_env_var_as_solana_swap():
    """Not a guess at the URL shape: the two verbs must not drift into
    different ideas of what 'pinned' means."""
    src = open("tools/defi/spl_deploy_verb.py").read()
    assert 'getenv("DEFI_SOLANA_RPC"' in src
    assert "api.mainnet-beta" not in src


# ==========================================================================
# Metadata — the half a buyer actually reads (042 audit)
# ==========================================================================

def test_a_clean_read_back_reports_the_ON_CHAIN_name(monkeypatch):
    monkeypatch.setattr("core.wallet.solana_onchain._rpc", lambda m, p: _info())
    text = V._prove_fixed_supply(MINT, SUPPLY_RAW, 9)
    assert "'Rob Probe'" in text and "'RPROBE'" in text
    assert "metadata authorities all null" in text


def test_a_mint_with_NO_metadata_is_called_out(monkeypatch):
    """A token with no on-chain name is the 'Unknown token' this path exists to
    avoid. Shipping one silently is worse than failing."""
    monkeypatch.setattr("core.wallet.solana_onchain._rpc",
                        lambda m, p: _info(extensions=[]))
    text = V._prove_fixed_supply(MINT, SUPPLY_RAW, 9)
    assert "VERIFIED AND WRONG" in text
    assert "NO on-chain metadata" in text


def test_a_SURVIVING_metadata_update_authority_is_called_out(monkeypatch):
    """Supply fixed but the NAME still editable is only half immutable, and the
    half that moves is the half a buyer reads."""
    monkeypatch.setattr("core.wallet.solana_onchain._rpc", lambda m, p: _info(
        extensions=[{"extension": "tokenMetadata",
                     "state": {"name": "x", "symbol": "y", "uri": "",
                               "updateAuthority": PAYER,
                               "additionalMetadata": []}}]))
    text = V._prove_fixed_supply(MINT, SUPPLY_RAW, 9)
    assert "VERIFIED AND WRONG" in text
    assert "the name can be changed" in text


def test_the_name_defaults_to_the_symbol_rather_than_being_blank():
    from core.wallet import spl_token
    from solders.hash import Hash
    tx, _kp, _mint, _ata = spl_token.build_fixed_supply_mint(
        payer=PAYER, decimals=9, supply_raw=SUPPLY_RAW,
        recent_blockhash=str(solders.hash.Hash.default()),
        mint_rent=spl_token.rent_exempt_lamports(spl_token.MINT_WITH_POINTER_LEN),
        name="RPROBE", symbol="RPROBE")
    meta = bytes(tx.message.instructions[3].data)
    assert b"RPROBE" in meta


def test_a_uri_is_optional_but_the_name_is_not():
    from core.wallet import spl_token
    with pytest.raises(spl_token.SplBuildError):
        spl_token.build_fixed_supply_mint(
            payer=PAYER, decimals=9, supply_raw=SUPPLY_RAW,
            recent_blockhash=str(solders.hash.Hash.default()),
            mint_rent=1, name="", symbol="")
