"""`solana_swap`: an UNOBSERVED declared mint must refuse, and a broadcast must
be CONFIRMED before it is reported.

Three verified findings, all of the same family — a check that reads as passing
when it never actually ran:

* **Token-2022 outflow bypass.** The ATA was derived under the classic SPL Token
  program only, so a Token-2022 mint's account was never in the simulation's
  address set. `in_delta = deltas.token_deltas.get(token_in)` then returned
  None, and `if in_delta is not None and in_delta < 0` SKIPPED the "more is
  leaving than you declared" refusal. The address derivation is fixed in
  `core/wallet/solana_tx_inspect.py`; this file pins the fail-CLOSED behaviour
  that makes the class of bug impossible rather than merely unlikely.
* **Unvetted transaction bytes.** The simulation is the choke point where the
  bytes about to be signed get decoded and vetted.
* **Unconfirmed broadcast.** `SolanaRail.confirm()` existed with no caller:
  the verb recorded the full spend and returned `BROADCAST: <sig>` with no idea
  whether the transaction landed, reverted, or expired.
"""
import types

import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from tools.defi.trade_tool import DefiTradeTool, SolanaSwapParams

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
MEME = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
ME = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"


class _Gate:
    has_daily_cap = True

    def __init__(self):
        self.recorded = []

    def check(self, *, venue, amount_usd, idempotency_key):
        return types.SimpleNamespace(allowed=True, reason=None)

    def record(self, **kw):
        self.recorded.append(kw)

    def reserve(self):
        class _C:
            async def __aenter__(s): return None
            async def __aexit__(s, *a): return False
        return _C()


class _Signer:
    address = ME

    def sign_transaction(self, tx):
        return tx


class _Wallet:
    def __init__(self):
        self.policy = _Gate()

    def solana_signer(self, account=0):
        return _Signer()

    @property
    def solana_address(self):
        return ME


def _quote(out=10_000_000, floor=9_900_000):
    from tools.defi.providers.jupiter import JupiterQuote
    return JupiterQuote(chain="solana", token_in=USDC, token_out=WSOL,
                        amount_in_raw=1_000_000, amount_out_raw=out,
                        amount_out_min_raw=floor, venue="jupiter:Orca",
                        raw={"outAmount": str(out),
                             "otherAmountThreshold": str(floor)})


def _params(**kw):
    base = dict(token_in=USDC, token_out=WSOL, amount_in=1.0, max_spend_usd=2.0)
    base.update(kw)
    return SolanaSwapParams(**base)


def _tool(wallet=None, *, deltas=None, **kw):
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = deltas if deltas is not None else SolanaDeltas(
        ok=True, token_deltas={USDC: -1_000_000})
    defaults = dict(
        wallet=wallet or _Wallet(),
        solana_decimals_fn=lambda m: 6,
        solana_quote_fn=lambda *a, **k: _quote(),
        solana_build_fn=lambda *a, **k: b"\x01",
        solana_simulate_fn=lambda **k: deltas,
    )
    defaults.update(kw)
    return DefiTradeTool(**defaults)


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    for var in ("DEFI_AUTONOMOUS_TURN_TRADING", "DEFI_MONITOR_EXITS",
                "DEFI_SOLANA_RPC"):
        monkeypatch.delenv(var, raising=False)


# -- finding 1b: an unobserved DECLARED mint fails CLOSED -------------------

@pytest.mark.asyncio
async def test_an_unobserved_outflow_token_refuses():
    """The Token-2022 shape: SOME token moved (so the empty-delta refusal does
    not fire) but the DECLARED outflow mint has no observation at all. That is
    the outflow assertion silently not running — the one check that bounds how
    much may leave — so it must refuse, not proceed."""
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, token_deltas={USDC: 4_000_000})
    tool = _tool(deltas=deltas, price_fn=lambda c, a: 1.0)
    res = await tool.solana_swap(
        _params(token_in=MEME, token_out=USDC, amount_in=1.0,
                max_spend_usd=5.0, dry_run=True))
    assert res.error, "an unobserved declared mint must never pass"
    assert "could not observe" in res.error
    assert MEME in res.error
    assert "nothing was broadcast" in res.error.lower()


@pytest.mark.asyncio
async def test_an_observed_outflow_token_still_passes():
    """The fail-closed rule must not refuse an ordinary, fully observed swap."""
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, token_deltas={MEME: -1_000_000,
                                                 USDC: 4_000_000})
    tool = _tool(deltas=deltas, price_fn=lambda c, a: 1.0)
    res = await tool.solana_swap(
        _params(token_in=MEME, token_out=USDC, amount_in=1.0,
                max_spend_usd=5.0, dry_run=True))
    assert res.error is None, res.error


# -- finding 1a/2: the real simulate path vets and widens ------------------

def test_the_simulation_path_derives_atas_for_both_token_programs():
    """The derivation moved to core/wallet/solana_tx_inspect.py; the invariant
    (never the classic program alone) travels with it."""
    import inspect
    from core.wallet import solana_tx_inspect
    src = inspect.getsource(solana_tx_inspect)
    assert "SPL_TOKEN_PROGRAMS" in src
    assert "candidate_atas" in src


def test_the_tool_delegates_simulation_to_the_vetting_path():
    import inspect
    src = inspect.getsource(DefiTradeTool._solana_simulate)
    assert "solana_tx_inspect" in src, (
        "the simulation must run through the module that vets the transaction "
        "bytes and names every wallet-owned token account")


# -- finding 3: a broadcast is confirmed, never assumed ---------------------

@pytest.mark.asyncio
async def test_a_confirmed_broadcast_reports_confirmed(monkeypatch):
    monkeypatch.setenv("DEFI_SOLANA_RPC", "https://solana.example/rpc")
    seen = []
    tool = _tool(solana_send_fn=lambda raw: "sig123",
                 solana_confirm_fn=lambda sig: seen.append(sig) or (True, "finalized"))
    res = await tool.solana_swap(_params(dry_run=False))
    assert res.error is None
    assert seen == ["sig123"]
    text = res.extracted_content or ""
    assert "RESULT: CONFIRMED" in text
    assert "sig123" in text


@pytest.mark.asyncio
async def test_a_reverted_broadcast_never_reads_as_a_success(monkeypatch):
    monkeypatch.setenv("DEFI_SOLANA_RPC", "https://solana.example/rpc")
    tool = _tool(solana_send_fn=lambda raw: "sigrev",
                 solana_confirm_fn=lambda sig: (
                     False, "landed but FAILED on-chain: err={'InstructionError': []}. "
                            "The fee was still paid."))
    res = await tool.solana_swap(_params(dry_run=False))
    text = res.extracted_content or ""
    assert "REVERTED ON-CHAIN" in text
    assert "CONFIRMED" not in text.replace("NOT CONFIRMED", "")


@pytest.mark.asyncio
async def test_an_expired_blockhash_reads_differently_from_a_revert(monkeypatch):
    """`null` status is UNKNOWN, not failure — most often an expired blockhash.
    It must never be reported as landed-and-failed, and never auto-resent."""
    monkeypatch.setenv("DEFI_SOLANA_RPC", "https://solana.example/rpc")
    sends = []
    tool = _tool(solana_send_fn=lambda raw: sends.append(raw) or "sigunk",
                 solana_confirm_fn=lambda sig: (
                     False, "UNKNOWN — the cluster has not seen this signature. "
                            "Most often the blockhash expired and it never landed; "
                            "do NOT resend without checking."))
    res = await tool.solana_swap(_params(dry_run=False))
    text = res.extracted_content or ""
    assert "NOT CONFIRMED" in text
    assert "REVERTED" not in text
    assert len(sends) == 1, "an unknown outcome must never be auto-resent"


@pytest.mark.asyncio
async def test_the_spend_is_recorded_even_when_confirmation_is_unknown(monkeypatch):
    """The money may have moved — an unconfirmed broadcast still counts against
    the cap. Losing the record would let a retry double-spend the headroom."""
    monkeypatch.setenv("DEFI_SOLANA_RPC", "https://solana.example/rpc")
    wallet = _Wallet()
    tool = _tool(wallet, solana_send_fn=lambda raw: "sigunk",
                 solana_confirm_fn=lambda sig: (False, "UNKNOWN — not seen"))
    await tool.solana_swap(_params(dry_run=False))
    assert wallet.policy.recorded
    assert wallet.policy.recorded[0]["result_ref"] == "sigunk"
