"""Finalization pass on the self-contained x402 rail (2026-08-24 audit).

Four defects found by the pre-deploy review, each with a money consequence:

- CRIT-1 the watcher trusted `X402_SETTLEMENT_RPC` on ANY chain while pairing it
  with a chain-derived USDC address. A sepolia pin left over from testing plus a
  mainnet `X402_DEFAULT_CHAIN` scans the WRONG network, gets a valid empty log
  set (never an RPC error, so the fail-open retry never fires), advances the
  checkpoint, and reports "detect ON" — a real payer's mainnet transfer is never
  seen and the invoice lapses with a false "expired" notice to someone who paid.
- A4 `resolve_treasury_address()` used `wallet.address`, which is the
  OPERATIONAL venue — under `AGENT_WALLET_OPERATIONAL_VENUE=x402` that is a
  different key than the treasury address `polyrob wallet init` funds and the
  docs promise.
- A3 `/api/x402/pricing` still read the raw env var, so it advertised
  "Not configured" while the agent card, the 8004 file, invoices and the 402
  challenge all handed out the wallet-resolved address.
- A5 `polyrob doctor` re-implemented the resolver's precedence with a different
  boolean parser (blank read as false; `bool_env` reads blank as the default).
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing, onchain_probe
from modules.x402.settlement_watcher import SettlementWatcher, _resolve_scan_target
from core.wallet.onchain import USDC_BASE_MAINNET, USDC_BASE_SEPOLIA

TREASURY = "0xTreasuryAddress000000000000000000000001"
BASE_ID = 8453
SEPOLIA_ID = 84532


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "false")
    for var in ("X402_SETTLEMENT_RPC", "DEFI_EVM_RPC_BASE", "X402_DEFAULT_CHAIN"):
        monkeypatch.delenv(var, raising=False)


def _pad(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def _log(tx_hash, value_atomic, block, usdc):
    return {
        "address": usdc,
        "topics": [onchain_probe.TRANSFER_TOPIC,
                   _pad("0xPayer0000000000000000000000000000000099"), _pad(TREASURY)],
        "data": hex(value_atomic),
        "blockNumber": hex(block),
        "transactionHash": tx_hash,
    }


class _FakeChain:
    """RPC fake that reports a chain id — a real endpoint always does."""

    def __init__(self, chain_id=BASE_ID, head=1000):
        self.chain_id = chain_id
        self.head = head
        self.logs = []
        self.get_logs_calls = 0
        self.chain_id_calls = 0

    def rpc(self, method, params):
        if method == "eth_chainId":
            self.chain_id_calls += 1
            if self.chain_id is None:
                raise RuntimeError("eth_chainId unavailable")
            return hex(self.chain_id)
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getLogs":
            self.get_logs_calls += 1
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            return [g for g in self.logs if lo <= int(g["blockNumber"], 16) <= hi]
        raise AssertionError(f"unexpected RPC method {method}")


class _WakeAgent:
    def __init__(self):
        self.wakes = []

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.wakes.append((session_id, user_id, text, metadata))
        return True


# --- CRIT-1: the scan must verify it is on the network it thinks it is -------

def test_resolve_scan_target_carries_the_expected_chain_id():
    # Assert against the chain registry rather than a literal URL: which RPC
    # `base` maps to is the registry's contract (and its own test's), while
    # THIS test owns the asset+chain-id pairing.
    from core.wallet.onchain import rpc_url_for_chain
    assert _resolve_scan_target("base") == (rpc_url_for_chain("base"),
                                            USDC_BASE_MAINNET, BASE_ID)
    assert _resolve_scan_target("base-sepolia") == ("https://sepolia.base.org",
                                                    USDC_BASE_SEPOLIA, SEPOLIA_ID)
    assert _resolve_scan_target("polygon") is None


@pytest.mark.asyncio
async def test_wrong_network_rpc_never_scans_and_never_advances(tmp_path, monkeypatch):
    """A sepolia RPC pinned while the configured chain is mainnet must NOT be
    scanned: an empty log set from the wrong chain is indistinguishable from
    'nobody paid', which would strand a real payment."""
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("X402_SETTLEMENT_RPC", "https://sepolia.base.org")
    db = await _setup_db(tmp_path)
    try:
        chain = _FakeChain(chain_id=SEPOLIA_ID)   # override points at testnet
        watcher = SettlementWatcher(_WakeAgent(), db=db, rpc_call=chain.rpc)
        out = await watcher.tick_once()
        assert out["onchain_settled"] == 0
        assert chain.get_logs_calls == 0
        assert await invoicing.get_scan_checkpoint(TREASURY.lower(), db=db) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_matching_network_scans_normally(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s_ok", amount_usd=4.44, purpose="p", db=db)
        chain = _FakeChain(chain_id=BASE_ID, head=1000)
        watcher = SettlementWatcher(_WakeAgent(), db=db, rpc_call=chain.rpc)
        await watcher.tick_once()          # seed
        chain.head = 1010
        chain.logs = [_log("0xok1", 4_440000, 1005, USDC_BASE_MAINNET)]
        out = await watcher.tick_once()
        assert out["onchain_settled"] == 1
        row = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id = ?", (inv["request_id"],))
        assert row["status"] == "completed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_chain_id_probed_once_not_every_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    db = await _setup_db(tmp_path)
    try:
        chain = _FakeChain(chain_id=BASE_ID)
        watcher = SettlementWatcher(_WakeAgent(), db=db, rpc_call=chain.rpc)
        await watcher.tick_once()
        await watcher.tick_once()
        await watcher.tick_once()
        assert chain.chain_id_calls == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unverifiable_chain_id_holds_off_rather_than_guessing(tmp_path, monkeypatch):
    """An RPC that cannot answer eth_chainId is not scanned — but the cursor is
    held, so a transient outage retries instead of burning the range."""
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    db = await _setup_db(tmp_path)
    try:
        chain = _FakeChain(chain_id=None)
        watcher = SettlementWatcher(_WakeAgent(), db=db, rpc_call=chain.rpc)
        out = await watcher.tick_once()
        assert out["onchain_settled"] == 0
        assert chain.get_logs_calls == 0
        assert await invoicing.get_scan_checkpoint(TREASURY.lower(), db=db) is None
    finally:
        await db.close()


# --- A4: the treasury VENUE address, not the operational one ----------------

class _VenueWallet:
    """Mirrors AgentWallet: `.address` is the OPERATIONAL venue; `signer_for`
    resolves a specific one."""

    class _Signer:
        def __init__(self, addr):
            self.address = addr

    def __init__(self, operational, treasury):
        self.address = operational
        self._treasury = treasury

    def signer_for(self, venue):
        assert venue == "treasury"
        return self._Signer(self._treasury)


def test_resolver_uses_the_treasury_venue_not_the_operational_venue(monkeypatch):
    monkeypatch.delenv("X402_PAYMENT_RECIPIENT", raising=False)
    import core.wallet.factory as factory
    monkeypatch.setattr(
        factory, "get_agent_wallet",
        lambda: _VenueWallet(operational="0xX402Venue", treasury="0xTreasuryVenue"))
    from modules.x402.x402_integration import resolve_treasury_address
    assert resolve_treasury_address() == "0xTreasuryVenue"


def test_resolver_falls_back_to_address_when_no_venue_accessor(monkeypatch):
    """A wallet double without `signer_for` must still resolve (fail-soft)."""
    monkeypatch.delenv("X402_PAYMENT_RECIPIENT", raising=False)

    class _Plain:
        address = "0xPlainAddr"

    import core.wallet.factory as factory
    monkeypatch.setattr(factory, "get_agent_wallet", lambda: _Plain())
    from modules.x402.x402_integration import resolve_treasury_address
    assert resolve_treasury_address() == "0xPlainAddr"


# --- A3: /pricing must advertise the same address as every other surface ----

def test_pricing_endpoint_uses_the_resolver(monkeypatch):
    monkeypatch.delenv("X402_PAYMENT_RECIPIENT", raising=False)
    monkeypatch.delenv("X402_PAYMENT_ADDRESS", raising=False)

    class _Plain:
        address = "0xWalletTreasury"

        def signer_for(self, venue):
            return self

    import core.wallet.factory as factory
    monkeypatch.setattr(factory, "get_agent_wallet", lambda: _Plain())

    from api.x402_endpoints import _pricing_recipient
    assert _pricing_recipient() == "0xWalletTreasury"
