"""Best-effort on-chain balance reads for the agent wallet (fail-open).

Shared by `polyrob wallet` (CLI) and the agent's `x402_wallet_status`/`self_status`
tools so the AGENT can SEE its own funds — the 2026-07-08 gap where a wallet holding
$10 USDC reported "$0" because no read path surfaced the on-chain balance. Public RPCs,
read-only, short timeout, never raises.
"""
from __future__ import annotations

import json as _json
import urllib.request

# Canonical USDC ERC-20 contract addresses (6 decimals) on Base. Named
# constants (not just inlined in _CHAIN below) so tools/x402/real_client.py's
# asset-pin gate can import and reuse the SAME mainnet address this module
# already trusts for on-chain balance reads, rather than re-declaring a
# second copy that could drift. USDC_BASE_SEPOLIA is the Base Sepolia
# TESTNET deployment — not used by the balance-read table below (_CHAIN is
# mainnet-only) but needed by the x402 client's testnet asset-pin.
USDC_BASE_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

# venue → chain it settles on (mainnet). Same EOA address on every EVM chain.
VENUE_CHAIN = {"treasury": "base", "x402": "base",
               "hyperliquid": "arbitrum", "polymarket": "polygon"}
# chain → (rpc, USDC erc20 contract [6 decimals], native symbol)
_CHAIN = {
    "base": ("https://mainnet.base.org", USDC_BASE_MAINNET, "ETH"),
    "arbitrum": ("https://arb1.arbitrum.io/rpc", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "ETH"),
    "polygon": ("https://polygon-rpc.com", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "POL"),
}
_ERC20_BALANCEOF = "0x70a08231000000000000000000000000"  # balanceOf(address) selector + pad


def _rpc(url: str, method: str, params: list, timeout: float = 4.0):
    body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    # Cloudflare-fronted public RPCs 403 the default python-urllib UA — send a normal one.
    req = urllib.request.Request(url, data=body, headers={
        "content-type": "application/json", "user-agent": "polyrob-wallet/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec - public read-only RPC
        return _json.loads(r.read()).get("result")


def venue_chain(venue: str):
    return VENUE_CHAIN.get(venue)


def balances(address: str, chain: str, timeout: float = 4.0):
    """(native, usdc) as floats, or (None, None) on any failure. Best-effort/fail-open."""
    cfg = _CHAIN.get(chain)
    if not cfg:
        return None, None
    rpc, usdc, _sym = cfg

    def _hex_int(raw):
        return int(raw, 16) if raw and raw != "0x" else 0

    native = usdc_bal = None
    try:
        native = _hex_int(_rpc(rpc, "eth_getBalance", [address, "latest"], timeout)) / 1e18
    except Exception:
        pass
    try:
        data = _ERC20_BALANCEOF + address[2:].lower()
        usdc_bal = _hex_int(_rpc(rpc, "eth_call", [{"to": usdc, "data": data}, "latest"], timeout)) / 1e6
    except Exception:
        pass
    return native, usdc_bal
