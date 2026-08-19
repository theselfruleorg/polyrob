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
    def rpc(chain, method, params, timeout=8.0):
        if method == "eth_chainId":
            return hex(1)          # RPC claims mainnet
        raise AssertionError(f"unexpected {method}")

    monkeypatch.setattr(rail, "_rpc_call", rpc)
    r = rail.EvmRail(chain="base", signer=_signer())
    assert r.chain_id == 8453, "config pins the chain; the RPC does not"


def test_rail_refuses_a_chain_it_does_not_know():
    with pytest.raises(ValueError):
        rail.EvmRail(chain="nosuchchain", signer=_signer())


# --------------------------------------------------------------------------
# Multi-chain (2026-08-17): the rail reads the registry, not a base-only table
# --------------------------------------------------------------------------

def test_rail_builds_for_ethereum_with_its_own_pinned_id():
    r = rail.EvmRail(chain="ethereum", signer=_signer())
    assert r.chain_id == 1


def test_rail_refuses_a_data_only_chain_at_construction():
    """Robinhood is readable, but no swap route or price feed is verified
    there — a rail that cannot build an honest transaction refuses early."""
    with pytest.raises(ValueError, match="robinhood"):
        rail.EvmRail(chain="robinhood", signer=_signer())


def test_the_rail_reads_the_endpoint_for_ITS_chain_not_base(monkeypatch):
    """The whole multi-chain hazard in one test: every read used to resolve the
    BASE endpoint, so an ethereum rail would take base's nonce and base's fees
    and sign them for chain 1."""
    seen = {}

    def fake_rpc(url, method, params, timeout=8.0):
        seen["url"] = url
        return hex(1) if method == "eth_chainId" else None

    monkeypatch.setattr(onchain, "_rpc", fake_rpc)
    monkeypatch.setenv("DEFI_EVM_RPC_ETHEREUM", "https://pinned.eth.example/rpc")
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", "https://pinned.base.example/rpc")
    ok, _ = rail.EvmRail(chain="ethereum", signer=_signer()).preflight()
    assert ok is True
    assert seen["url"] == "https://pinned.eth.example/rpc"


def test_the_fee_ceiling_is_the_chains_own(monkeypatch):
    """One L2-sized ceiling refused honest L1 transactions. A fee that base
    rejects must be signable on ethereum, whose ceiling is its own."""
    fees = {
        "eth_chainId": hex(1),
        "eth_getTransactionCount": hex(2),
        "eth_maxPriorityFeePerGas": hex(10 ** 9),
        "eth_getBlockByNumber": {"baseFeePerGas": hex(10 ** 10)},
    }
    monkeypatch.setattr(rail, "_rpc_call",
                        lambda c, m, p, timeout=8.0: fees[m])
    # 120k gas * (2*1e10 + 1e9) = 2.52e15 wei: above base's 2e15, below eth's 1e16.
    tx = rail.EvmRail(chain="ethereum", signer=_signer()).build_erc20_transfer(
        token=USDC, to=TO, amount_raw=1)
    assert tx["chainId"] == 1

    fees["eth_chainId"] = hex(8453)
    with pytest.raises(rail.GasCeilingExceeded):
        rail.EvmRail(chain="base", signer=_signer()).build_erc20_transfer(
            token=USDC, to=TO, amount_raw=1)


def test_rail_verifies_the_rpc_serves_the_expected_chain(monkeypatch):
    """Pinning is not enough: broadcasting to a node on ANOTHER chain would put
    a validly-signed base tx nowhere useful. Verify and refuse."""
    monkeypatch.setattr(rail, "_rpc_call",
                        lambda c, m, p, timeout=8.0: hex(1) if m == "eth_chainId" else None)
    r = rail.EvmRail(chain="base", signer=_signer())
    ok, why = r.preflight()
    assert ok is False
    assert "chain" in why.lower()


# --------------------------------------------------------------------------
# Gas policy
# --------------------------------------------------------------------------

def test_absurd_fee_is_refused(monkeypatch):
    monkeypatch.setattr(rail, "_rpc_call", lambda c, m, p, timeout=8.0: {
        "eth_chainId": hex(8453),
        "eth_getTransactionCount": hex(3),
        "eth_maxPriorityFeePerGas": hex(10**9),
        "eth_getBlockByNumber": {"baseFeePerGas": hex(10**18)},   # absurd
    }[m])
    r = rail.EvmRail(chain="base", signer=_signer())
    with pytest.raises(rail.GasCeilingExceeded):
        r.build_erc20_transfer(token=USDC, to=TO, amount_raw=1)


def test_gas_fields_are_eip1559(monkeypatch):
    monkeypatch.setattr(rail, "_rpc_call", lambda c, m, p, timeout=8.0: {
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
# Gas sizing from the simulation (§3a, 2026-08-15)
# --------------------------------------------------------------------------
# A fixed 120k limit out-of-gas-reverts a Uniswap V3 swap (130-190k with tick
# crossings) ON-CHAIN AND BURNS THE FEE — the exact failure mode the guard
# exists to avoid. The rail must size the limit from the simulation's gasUsed
# with a margin, and re-check the fee ceiling against the sized gas.

def _rpc_for_build(monkeypatch, *, base_fee=10**8, tip=10**8):
    monkeypatch.setattr(rail, "_rpc_call", lambda c, m, p, timeout=8.0: {
        "eth_chainId": hex(8453),
        "eth_getTransactionCount": hex(7),
        "eth_maxPriorityFeePerGas": hex(tip),
        "eth_getBlockByNumber": {"baseFeePerGas": hex(base_fee)},
    }[m])


def _built_tx(monkeypatch, **kw):
    _rpc_for_build(monkeypatch, **kw)
    r = rail.EvmRail(chain="base", signer=_signer())
    return r, r.build_erc20_transfer(token=USDC, to=TO, amount_raw=1)


def test_size_gas_applies_the_simulation_margin(monkeypatch):
    r, tx = _built_tx(monkeypatch)
    sized = r.size_gas(tx, 140_000)
    assert sized["gas"] == 210_000, "gasUsed x1.5"


def test_size_gas_without_a_measurement_keeps_the_default(monkeypatch):
    r, tx = _built_tx(monkeypatch)
    assert r.size_gas(tx, None)["gas"] == rail.DEFAULT_GAS_LIMIT


def test_size_gas_caps_the_margin_at_the_max_gas_limit(monkeypatch):
    r, tx = _built_tx(monkeypatch)
    assert r.size_gas(tx, 400_000)["gas"] == rail.MAX_GAS_LIMIT


def test_size_gas_refuses_a_tx_the_cap_cannot_hold(monkeypatch):
    """If the simulation ALREADY needs more than the cap, broadcasting with
    the cap would out-of-gas revert and burn the fee — refuse instead."""
    r, tx = _built_tx(monkeypatch)
    with pytest.raises(rail.GasCeilingExceeded):
        r.size_gas(tx, rail.MAX_GAS_LIMIT + 1)


def test_size_gas_rechecks_the_fee_ceiling_with_the_sized_gas(monkeypatch):
    """build passed the ceiling at the default 120k; the sized 180k must be
    re-checked, not inherited."""
    # max_fee = 2*base_fee + tip = 1.5e10: 120k*1.5e10 = 1.8e15 <= 2e15 ok,
    # 180k*1.5e10 = 2.7e15 > 2e15 -> refuse.
    r, tx = _built_tx(monkeypatch, base_fee=7_450_000_000, tip=100_000_000)
    with pytest.raises(rail.GasCeilingExceeded):
        r.size_gas(tx, 120_000)


# --------------------------------------------------------------------------
# Receipt honesty
# --------------------------------------------------------------------------

def test_reverted_receipt_is_failed_not_success(monkeypatch):
    monkeypatch.setattr(rail, "_rpc_call", lambda c, m, p, timeout=8.0: {
        "eth_chainId": hex(8453),
        "eth_getTransactionReceipt": {"status": "0x0", "blockNumber": hex(10),
                                      "gasUsed": hex(21000)},
    }[m])
    r = rail.EvmRail(chain="base", signer=_signer())
    rec = r.await_receipt("0x" + "ab" * 32, timeout=1.0, poll_interval=0.0)
    assert rec.status == "failed"
    assert rec.succeeded is False


def test_successful_receipt(monkeypatch):
    monkeypatch.setattr(rail, "_rpc_call", lambda c, m, p, timeout=8.0: {
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
    monkeypatch.setattr(rail, "_rpc_call", lambda c, m, p, timeout=8.0:
                        hex(8453) if m == "eth_chainId" else None)
    r = rail.EvmRail(chain="base", signer=_signer())
    rec = r.await_receipt("0x" + "ef" * 32, timeout=0.05, poll_interval=0.01)
    assert rec.status == "pending"
    assert rec.succeeded is False
    assert rec.tx_hash.startswith("0x")


def test_a_read_only_chain_is_refused_at_construction():
    """Arbitrum is a KNOWN chain (venue balances are read there) but nothing
    about its money path is verified, so no rail may exist for it. Was
    "unknown chain" when the rail was base-only; the refusal survives the
    multi-chain rewrite, only its reason got more precise."""
    with pytest.raises(ValueError, match="read-only"):
        rail.EvmRail(chain="arbitrum", signer=_signer())
