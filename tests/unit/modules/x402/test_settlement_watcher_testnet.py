"""W1.2/W1.3 (x402 self-contained rail, 2026-08-21): on-chain settlement
detection on base-sepolia + operator-pinnable scan RPC resolution.

- `base-sepolia` (and its aliases) is a scannable chain: the full
  scan -> match -> settle -> wake path works on testnet, so the Tier-1
  acceptance run can happen there per the house test rule.
- `_resolve_scan_target(chain)` is the ONE (rpc_url, usdc_addr) resolver:
  `X402_SETTLEMENT_RPC` beats `DEFI_EVM_RPC_<CHAIN>` beats the built-in
  default; an unsupported chain resolves to None (refused, never guessed).
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing, onchain_probe, settlement_watcher
from modules.x402.settlement_watcher import SettlementWatcher, _resolve_scan_target
from core.wallet.onchain import USDC_BASE_MAINNET, USDC_BASE_SEPOLIA

TREASURY = "0xTreasuryAddress000000000000000000000001"


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
    for var in ("X402_SETTLEMENT_RPC", "DEFI_EVM_RPC_BASE",
                "X402_SETTLEMENT_SCAN_MAX_SPAN", "X402_SETTLEMENT_CONFIRMATIONS"):
        monkeypatch.delenv(var, raising=False)


def _pad(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def _log(tx_hash, from_addr, value_atomic, block, usdc, to_addr=TREASURY):
    return {
        "address": usdc,
        "topics": [onchain_probe.TRANSFER_TOPIC, _pad(from_addr), _pad(to_addr)],
        "data": hex(value_atomic),
        "blockNumber": hex(block),
        "transactionHash": tx_hash,
    }


class _FakeChain:
    def __init__(self, head=1000, chain_id=84532):
        self.head = head
        self.chain_id = chain_id
        self.logs = []
        self.get_logs_calls = 0

    def rpc(self, method, params):
        if method == "eth_chainId":
            # The scan verifies it is on the network its USDC address belongs
            # to before reading logs (2026-08-24 audit) — a real RPC always
            # answers this.
            return hex(self.chain_id)
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getLogs":
            self.get_logs_calls += 1
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            return [log for log in self.logs if lo <= int(log["blockNumber"], 16) <= hi]
        raise AssertionError(f"unexpected RPC method {method}")


class _WakeAgent:
    def __init__(self):
        self.wakes = []

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.wakes.append((session_id, user_id, text, metadata))
        return True


# --- W1.2: sepolia is scannable end-to-end ----------------------------------

@pytest.mark.asyncio
async def test_sepolia_matched_transfer_settles_and_wakes(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base-sepolia")
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_sep", amount_usd=3.21, purpose="test",
            db=db)

        chain = _FakeChain(head=1000)
        agent = _WakeAgent()
        watcher = SettlementWatcher(agent, db=db, rpc_call=chain.rpc,
                                    usdc_addr=USDC_BASE_SEPOLIA)
        await watcher.tick_once()  # seed only

        chain.head = 1010
        chain.logs = [_log("0xsep1", "0xPayer0000000000000000000000000000000011",
                           3_210000, 1005, USDC_BASE_SEPOLIA)]
        out = await watcher.tick_once()

        assert out["onchain_settled"] == 1
        assert len(agent.wakes) == 1
        row = await db.fetch_one(
            "SELECT status, transaction_hash FROM x402_payment_requests WHERE id = ?",
            (inv["request_id"],))
        assert row["status"] == "completed"
        assert row["transaction_hash"] == "0xsep1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unsupported_chain_still_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "polygon")
    db = await _setup_db(tmp_path)
    try:
        chain = _FakeChain()
        out = await SettlementWatcher(
            _WakeAgent(), db=db, rpc_call=chain.rpc,
            usdc_addr=USDC_BASE_MAINNET).tick_once()
        assert out["onchain_settled"] == 0
        assert chain.get_logs_calls == 0
    finally:
        await db.close()


# --- W1.3: scan target resolution -------------------------------------------

def test_resolve_base_default_uses_chain_row_rpc(monkeypatch):
    # The chain row is the SSOT for base's RPC (its literal value belongs to
    # core/wallet/chains.py's own test); here we assert the wiring.
    #
    # 046: the target no longer carries an ASSET. The token address comes from
    # the pending invoice's own asset row, because one target could only ever
    # describe one token — which is why detection on any other asset was
    # structurally impossible before.
    from core.wallet.onchain import rpc_url_for_chain
    url, cid = _resolve_scan_target("base")
    assert url == rpc_url_for_chain("base")
    assert cid == 8453


def test_resolve_base_honors_defi_evm_rpc_base(monkeypatch):
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", "https://pinned.example/rpc")
    url, _cid = _resolve_scan_target("base")
    assert url == "https://pinned.example/rpc"


def test_resolve_sepolia_default_rpc(monkeypatch):
    """base-sepolia keeps its OWN branch: the money chain registry carries only
    chains verified for TRADING, so it has no testnet row, and the x402 module
    has always owned this RPC and chain id."""
    url, cid = _resolve_scan_target("base-sepolia")
    assert url == "https://sepolia.base.org"
    assert cid == 84532


def test_the_sepolia_asset_still_resolves_to_the_testnet_usdc():
    """The address moved OUT of the scan target and INTO the asset registry —
    it is still pinned, just in the one place that owns asset identity."""
    from core.payments.assets import resolve
    assert resolve("usdc-base-sepolia").address == USDC_BASE_SEPOLIA


def test_resolve_x402_settlement_rpc_beats_everything(monkeypatch):
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", "https://pinned.example/rpc")
    monkeypatch.setenv("X402_SETTLEMENT_RPC", "https://watcher.example/rpc")
    assert _resolve_scan_target("base")[0] == "https://watcher.example/rpc"
    assert _resolve_scan_target("base-sepolia")[0] == "https://watcher.example/rpc"


def test_resolve_unsupported_chain_is_none():
    """⚠️ 046 WIDENED this. `polygon` used to be unscannable only because the
    resolver hardcoded base; it is a verified EVM row in the chain registry and
    is now reachable. What stays unscannable is a chain with NO row (and a
    non-EVM row, which the EVM pass must never answer for)."""
    assert _resolve_scan_target("atlantis") is None
    assert _resolve_scan_target("") is None
    assert _resolve_scan_target("solana") is None
    assert _resolve_scan_target("polygon") is not None
