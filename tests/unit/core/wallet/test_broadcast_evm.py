"""EVM broadcast rail — the only path that can move value on-chain.

Every assertion here is a refusal or an honesty property. The rail must never
report a transaction as succeeded that did not, and must never sign for a chain
other than the one it was configured for.
"""
import pytest

from core.wallet import onchain
from core.wallet.broadcast import evm as rail
from core.wallet.signer import LocalEoaSigner

KEY = bytes.fromhex("59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")
TO = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _signer():
    return LocalEoaSigner(KEY)


# --------------------------------------------------------------------------
# Signing perimeter
# --------------------------------------------------------------------------

def test_signer_can_sign_a_transaction():
    s = _signer()
    raw = s.sign_transaction({
        "to": TO, "value": 0, "data": "0x", "nonce": 0,
        "gas": 21000, "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**8,
        "chainId": 8453,
    })
    assert isinstance(raw, bytes) and len(raw) > 32


def test_signing_a_transaction_without_chain_id_is_refused():
    """An unpinned chainId is EIP-155 replay exposure."""
    s = _signer()
    with pytest.raises(ValueError, match="chainId"):
        s.sign_transaction({"to": TO, "value": 0, "nonce": 0, "gas": 21000,
                            "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**8})


# --------------------------------------------------------------------------
# Chain pinning — never trust the RPC for identity
# --------------------------------------------------------------------------

def test_chain_id_comes_from_config_not_the_rpc(monkeypatch):
    """A repointed or hostile RPC must not decide which chain we sign for."""
    def rpc(method, params, timeout=8.0):
        if method == "eth_chainId":
            return hex(1)          # RPC claims mainnet
        raise AssertionError(f"unexpected {method}")

    monkeypatch.setattr(rail, "_rpc_call", rpc)
    r = rail.EvmRail(chain="base", signer=_signer())
    assert r.chain_id == 8453, "config pins the chain; the RPC does not"


def test_rail_refuses_a_chain_it_does_not_know():
    with pytest.raises(ValueError):
        rail.EvmRail(chain="nosuchchain", signer=_signer())


def test_rail_verifies_the_rpc_serves_the_expected_chain(monkeypatch):
    """Pinning is not enough: broadcasting to a node on ANOTHER chain would put
    a validly-signed base tx nowhere useful. Verify and refuse."""
    monkeypatch.setattr(rail, "_rpc_call",
                        lambda m, p, timeout=8.0: hex(1) if m == "eth_chainId" else None)
    r = rail.EvmRail(chain="base", signer=_signer())
    ok, why = r.preflight()
    assert ok is False
    assert "chain" in why.lower()


# --------------------------------------------------------------------------
# Gas policy
# --------------------------------------------------------------------------

def test_absurd_fee_is_refused(monkeypatch):
    monkeypatch.setattr(rail, "_rpc_call", lambda m, p, timeout=8.0: {
        "eth_chainId": hex(8453),
        "eth_getTransactionCount": hex(3),
        "eth_maxPriorityFeePerGas": hex(10**9),
        "eth_getBlockByNumber": {"baseFeePerGas": hex(10**18)},   # absurd
    }[m])
    r = rail.EvmRail(chain="base", signer=_signer())
    with pytest.raises(rail.GasCeilingExceeded):
        r.build_erc20_transfer(token=USDC, to=TO, amount_raw=1)


def test_gas_fields_are_eip1559(monkeypatch):
    monkeypatch.setattr(rail, "_rpc_call", lambda m, p, timeout=8.0: {
        "eth_chainId": hex(8453),
        "eth_getTransactionCount": hex(7),
        "eth_maxPriorityFeePerGas": hex(10**8),
        "eth_getBlockByNumber": {"baseFeePerGas": hex(10**8)},
        "eth_estimateGas": hex(60000),
    }[m])
    r = rail.EvmRail(chain="base", signer=_signer())
    tx = r.build_erc20_transfer(token=USDC, to=TO, amount_raw=250_000)
    assert tx["chainId"] == 8453
    assert tx["nonce"] == 7
    assert "maxFeePerGas" in tx and "maxPriorityFeePerGas" in tx
    assert "gasPrice" not in tx
    assert tx["to"] == USDC, "an ERC-20 transfer is sent TO the token contract"
    assert tx["data"].startswith("0xa9059cbb"), "transfer(address,uint256) selector"
    assert TO[2:].lower() in tx["data"].lower()


# --------------------------------------------------------------------------
# Receipt honesty
# --------------------------------------------------------------------------

def test_reverted_receipt_is_failed_not_success(monkeypatch):
    monkeypatch.setattr(rail, "_rpc_call", lambda m, p, timeout=8.0: {
        "eth_chainId": hex(8453),
        "eth_getTransactionReceipt": {"status": "0x0", "blockNumber": hex(10),
                                      "gasUsed": hex(21000)},
    }[m])
    r = rail.EvmRail(chain="base", signer=_signer())
    rec = r.await_receipt("0x" + "ab" * 32, timeout=1.0, poll_interval=0.0)
    assert rec.status == "failed"
    assert rec.succeeded is False


def test_successful_receipt(monkeypatch):
    monkeypatch.setattr(rail, "_rpc_call", lambda m, p, timeout=8.0: {
        "eth_chainId": hex(8453),
        "eth_getTransactionReceipt": {"status": "0x1", "blockNumber": hex(11),
                                      "gasUsed": hex(52000)},
    }[m])
    r = rail.EvmRail(chain="base", signer=_signer())
    rec = r.await_receipt("0x" + "cd" * 32, timeout=1.0, poll_interval=0.0)
    assert rec.succeeded is True
    assert rec.block_number == 11


def test_receipt_timeout_is_pending_not_success(monkeypatch):
    """Never 'sent, assume fine' — a pending tx is an open question."""
    monkeypatch.setattr(rail, "_rpc_call", lambda m, p, timeout=8.0:
                        hex(8453) if m == "eth_chainId" else None)
    r = rail.EvmRail(chain="base", signer=_signer())
    rec = r.await_receipt("0x" + "ef" * 32, timeout=0.05, poll_interval=0.01)
    assert rec.status == "pending"
    assert rec.succeeded is False
    assert rec.tx_hash.startswith("0x")
