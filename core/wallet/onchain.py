"""Best-effort on-chain balance reads for the agent wallet (fail-open).

Shared by `polyrob wallet` (CLI) and the agent's `x402_wallet_status`/`self_status`
tools so the AGENT can SEE its own funds — the 2026-07-08 gap where a wallet holding
$10 USDC reported "$0" because no read path surfaced the on-chain balance. Public RPCs,
read-only, short timeout, never raises.
"""
from __future__ import annotations

import json as _json
import os
import urllib.request


class RpcError(RuntimeError):
    """A JSON-RPC call failed, errored, or returned no result.

    Exists so a provider failure can never be mistaken for a value. Before this,
    `_rpc` returned `.get("result")` — a `{"error": ...}` body yielded None and
    the caller's hex parse turned that into 0, so a rate-limited RPC reported a
    CONFIRMED zero balance (audit 2026-08-07, P1-2). Unknown must stay unknown.
    """

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

# Multicall3 — the same address on every EVM chain it is deployed to. Pinned
# here rather than configurable: a swappable aggregator address would let a
# misconfiguration silently reroute every balance read.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
_SEL_AGGREGATE3 = "0x82ad56cb"  # aggregate3((address,bool,bytes)[])


def _word(n: int) -> str:
    return f"{n:064x}"


def _encode_aggregate3(calls) -> str:
    """Encode aggregate3 for a list of (target, calldata_hex) pairs.

    ``allowFailure`` is True for every call — a token that reverts on
    ``balanceOf`` must not abort the whole batch; it becomes an UNKNOWN entry.
    """
    n = len(calls)
    head = _word(32)                       # offset to the array
    body = _word(n)
    # Each tuple is dynamic (it carries `bytes`), so the array holds offsets.
    tuples = []
    for target, data in calls:
        payload = bytes.fromhex(data[2:])
        t = (_word(int(target, 16))         # address
             + _word(1)                     # allowFailure = true
             + _word(96)                    # offset to bytes within the tuple
             + _word(len(payload))
             + payload.hex().ljust(((len(payload) + 31) // 32) * 64, "0"))
        tuples.append(t)
    offset = 32 * n
    offsets = ""
    for t in tuples:
        offsets += _word(offset)
        offset += len(t) // 2
    return "0x" + _SEL_AGGREGATE3[2:] + head + body + offsets + "".join(tuples)


def _encode_aggregate3_result(pairs) -> str:
    """Encode an aggregate3 RETURN value — (bool success, bytes returnData)[].

    Test helper, kept beside the decoder so the two cannot drift.
    ``(True, int)`` encodes a 32-byte word; ``(_, None)`` encodes empty data.
    """
    n = len(pairs)
    tuples = []
    for success, value in pairs:
        data = b"" if value is None else int(value).to_bytes(32, "big")
        t = (_word(1 if success else 0)
             + _word(64)                    # offset to bytes within the tuple
             + _word(len(data))
             + (data.hex().ljust(64, "0") if data else ""))
        tuples.append(t)
    offset = 32 * n
    offsets = ""
    for t in tuples:
        offsets += _word(offset)
        offset += len(t) // 2
    return "0x" + _word(32) + _word(n) + offsets + "".join(tuples)


def _decode_aggregate3_result(raw: str, count: int):
    """Decode to a list of Optional[int], one per call.

    ``None`` means UNKNOWN — the sub-call reverted (``success == False``) or
    returned fewer than 32 bytes. It never means zero.
    """
    out = [None] * count
    if not raw or raw == "0x":
        return out
    data = bytes.fromhex(raw[2:])
    if len(data) < 64:
        return out
    n = min(int.from_bytes(data[32:64], "big"), count)
    for i in range(n):
        try:
            off = 64 + int.from_bytes(data[64 + i * 32:96 + i * 32], "big")
            success = int.from_bytes(data[off:off + 32], "big") == 1
            length = int.from_bytes(data[off + 64:off + 96], "big")
            if not success or length < 32:
                continue
            out[i] = int.from_bytes(data[off + 96:off + 96 + 32], "big")
        except Exception:
            continue
    return out


def token_balances(holder: str, chain: str, token_addresses, *, timeout: float = 8.0):
    """{checksummed token address -> raw units or None} for *holder*.

    ``None`` is UNKNOWN (the sub-call reverted, or the batch failed). A
    successful call returning 0 is a real zero and stays 0.
    """
    from core.wallet.tokens import normalize_address
    keys = [normalize_address(a) for a in token_addresses]
    if not keys:
        return {}
    if chain not in _CHAIN:
        return {k: None for k in keys}
    try:
        holder_pad = normalize_address(holder)[2:].lower().rjust(64, "0")
        calls = [(k, "0x70a08231" + holder_pad) for k in keys]
        raw = _rpc(rpc_url_for_chain(chain), "eth_call",
                   [{"to": MULTICALL3, "data": _encode_aggregate3(calls)}, "latest"], timeout)
        return dict(zip(keys, _decode_aggregate3_result(raw, len(keys))))
    except Exception:
        return {k: None for k in keys}


def rpc_url_for_chain(chain: str) -> str:
    """Operator-pinnable RPC endpoint for *chain*.

    Default is the historical public endpoint, so this is a no-op until an
    operator sets `DEFI_EVM_RPC_<CHAIN>`. The public endpoints are shared,
    rate-limited and unauthenticated; pinning a real one is the supported way to
    stop a provider hiccup from degrading every read.
    """
    cfg = _CHAIN.get(chain)
    default = cfg[0] if cfg else ""
    return os.getenv(f"DEFI_EVM_RPC_{chain.upper()}", "").strip() or default


def _rpc(url: str, method: str, params: list, timeout: float = 4.0):
    body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    # Cloudflare-fronted public RPCs 403 the default python-urllib UA — send a normal one.
    req = urllib.request.Request(url, data=body, headers={
        "content-type": "application/json", "user-agent": "polyrob-wallet/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec - public read-only RPC
        payload = _json.loads(r.read())
    if isinstance(payload, dict) and payload.get("error") is not None:
        raise RpcError(f"{method}: {payload['error']}")
    if not isinstance(payload, dict) or "result" not in payload:
        raise RpcError(f"{method}: response carried no result")
    return payload["result"]


def _hex_int(raw):
    """Hex quantity -> int, or None when the value is UNKNOWN.

    `None`/`"0x"` mean "no answer" and must not collapse to 0; `"0x0"` is a
    genuine on-chain zero and stays 0. Module-level (not nested) so the
    distinction is directly testable.
    """
    if raw is None or raw == "0x":
        return None
    return int(raw, 16)


def venue_chain(venue: str):
    return VENUE_CHAIN.get(venue)


def balances(address: str, chain: str, timeout: float = 4.0):
    """(native, usdc) as floats, or (None, None) on any failure. Best-effort/fail-open.

    None means UNKNOWN, never zero — every caller already branches on `is None`
    (tools/controller/action_registration.py, tools/x402/service.py,
    cli/commands/wallet.py, modules/credits/balances.py).
    """
    cfg = _CHAIN.get(chain)
    if not cfg:
        return None, None
    _default_rpc, usdc, _sym = cfg
    rpc = rpc_url_for_chain(chain)

    native = usdc_bal = None
    try:
        raw = _hex_int(_rpc(rpc, "eth_getBalance", [address, "latest"], timeout))
        native = None if raw is None else raw / 1e18
    except Exception:
        pass
    try:
        data = _ERC20_BALANCEOF + address[2:].lower()
        raw = _hex_int(_rpc(rpc, "eth_call", [{"to": usdc, "data": data}, "latest"], timeout))
        usdc_bal = None if raw is None else raw / 1e6
    except Exception:
        pass
    return native, usdc_bal
