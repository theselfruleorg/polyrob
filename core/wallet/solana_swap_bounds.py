"""Decode the Jupiter v6 ExactIn bounds that the program actually executes.

Wire layouts: Jupiter v6's on-chain Anchor IDL (see core/wallet/jupiter_v6_schema.py,
which pins every route-plan ``Swap`` variant). ``route`` and
``shared_accounts_route`` carry the route vector BEFORE the amounts;
``route_v2`` and ``shared_accounts_route_v2`` carry it AFTER them.
A swap variant newer than the pinned table, token-ledger routes and
unresolved mint accounts refuse. Parsing the route vector is essential: Anchor
permits trailing bytes, so reading the last 19 bytes alone would allow a forged
protective suffix.
"""
from dataclasses import dataclass
from hashlib import sha256
import struct

from core.wallet.jupiter_v6_schema import SWAP_VARIANTS, UnknownSwapVariant, skip_swap

V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
_ROUTE = sha256(b"global:route").digest()[:8]
_SHARED = sha256(b"global:shared_accounts_route").digest()[:8]
_ROUTE_V2 = sha256(b"global:route_v2").digest()[:8]
_SHARED_V2 = sha256(b"global:shared_accounts_route_v2").digest()[:8]

#: discriminator -> (shared, v2, authority, source mint or None, output mint,
#: wallet output account, optional alternate destination or None) — account
#: indexes from the IDL's account lists.
_LAYOUTS = {
    _ROUTE: (False, False, 1, None, 5, 3, 4),
    _SHARED: (True, False, 2, 7, 8, 6, None),
    _ROUTE_V2: (False, True, 0, 3, 4, 2, 7),
    _SHARED_V2: (True, True, 1, 6, 7, 5, None),
}


@dataclass(frozen=True)
class SwapBounds:
    token_in: str
    token_out: str
    amount_in_raw: int
    min_out_raw: int
    slippage_bps: int


def _route_plan(data: bytes, offset: int, *, v2: bool):
    """Parse the route vector; return (end offset, refusal or None)."""
    count = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    if not 1 <= count <= 64:
        return offset, "Jupiter route plan length is invalid"
    for _ in range(count):
        tag = data[offset]
        try:
            offset = skip_swap(data, offset)
        except UnknownSwapVariant:
            return offset, (
                f"Jupiter route uses swap variant {tag}, which is newer than the pinned "
                f"Jupiter v6 schema ({len(SWAP_VARIANTS)} variants); the schema in "
                "core/wallet/jupiter_v6_schema.py must be regenerated before this route can run")
        except ValueError:
            return offset, "Jupiter route contains an invalid swap step field"
        if v2:
            share, _, _ = struct.unpack_from("<HBB", data, offset)
            ok, offset = 1 <= share <= 10000, offset + 4
        else:
            share, _, _ = struct.unpack_from("<BBB", data, offset)
            ok, offset = 1 <= share <= 100, offset + 3
        if not ok:
            return offset, "Jupiter route contains an invalid allocation"
    return offset, None


def refusal(instructions, bounds: SwapBounds, *, owner: str) -> str | None:
    from core.wallet.solana_tx_inspect import JUPITER_PROGRAM_IDS, candidate_atas
    routes = [ix for ix in instructions if ix.program in JUPITER_PROGRAM_IDS]
    if len(routes) != 1 or routes[0].program != V6:
        return "swap requires one understood Jupiter v6 route"
    ix = routes[0]
    data = ix.data
    layout = _LAYOUTS.get(bytes(data[:8]))
    if layout is None:
        return "Jupiter route discriminator is not an understood ExactIn instruction"
    shared, v2, authority, mint_in, mint_out, wallet_out, alternate = layout
    try:
        offset = 9 if shared else 8  # shared authority id precedes the arguments
        if v2:
            amount_in, quoted_out, slippage, platform_fee, positive = struct.unpack_from(
                "<QQHHH", data, offset)
            end, why = _route_plan(data, offset + 22, v2=True)
        else:
            end, why = _route_plan(data, offset, v2=False)
            if not why and len(data) == end + 19:
                amount_in, quoted_out, slippage, platform_fee = struct.unpack_from(
                    "<QQHB", data, end)
                end, positive = end + 19, 0
        if why:
            return why
        if len(data) != end:
            return "Jupiter route has truncated or trailing instruction data"
        if amount_in != bounds.amount_in_raw:
            return "Jupiter instruction input does not match the requested amount"
        if not 0 <= slippage <= bounds.slippage_bps or platform_fee or positive:
            return "Jupiter instruction slippage or platform fee exceeds the approved bounds"
        floor = quoted_out * (10000 - slippage) // 10000
        if floor < bounds.min_out_raw or floor <= 0:
            return "Jupiter instruction minimum output is below the confirmed floor"
        if ix.accounts[authority] != owner or ix.accounts[mint_out] != bounds.token_out:
            return "Jupiter instruction authority or output mint is wrong or unresolved"
        if mint_in is not None and ix.accounts[mint_in] != bounds.token_in:
            return "Jupiter instruction input mint is wrong or unresolved"
        destination = ix.accounts[wallet_out]
        if destination not in candidate_atas(owner, bounds.token_out):
            return "Jupiter output account is not a verified associated account of the wallet"
        if alternate is not None and ix.accounts[alternate] not in (V6, destination):
            return "Jupiter alternate destination account is not the wallet's output account"
    except (IndexError, TypeError, struct.error):
        return "Jupiter instruction bounds could not be decoded"
    return None
