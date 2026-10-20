"""The on-chain floor inside a LI.FI diamond swap, read from the SIGNED bytes.

The swap rail asserts its output floor against a simulated receipt
(``TxIntent.min_inflow_raw``). A simulation can be told apart from the real
block, and an aggregator API can encode a minimum of 0 while its JSON reports a
healthy ``toAmountMin``. So for the LI.FI facets whose layout is known, the
minimum the CHAIN will enforce is decoded from the calldata and held to our
floor, and the receiver is held to our own wallet.

Every known facet takes ``(bytes32 transactionId, string integrator,
string referrer, address receiver, uint256 minAmount, SwapData…)``.

``decode`` returns ``None`` for an unknown selector. ``floor_refusal`` (the
guard's step 3a, run only for a call to the pinned diamond that declares an
output floor) REFUSES an unknown selector (085 W8 txguard LEAD-1): without a
decoded on-chain minimum the only floor left is the simulated one, and a
contract can tell a simulation apart from the block. When LI.FI ships a new
same-chain swap facet, add it to ``_FACETS``. A KNOWN selector whose body does
not decode raises ``ValueError`` — that is malformed, not new.
"""
import functools
from dataclasses import dataclass
from typing import Optional

from core.wallet import abi

_SWAP_DATA = {"type": "tuple", "components": [
    {"type": "address"}, {"type": "address"}, {"type": "address"}, {"type": "address"},
    {"type": "uint256"}, {"type": "bytes"}, {"type": "bool"}]}
_SWAP_DATA_LIST = dict(_SWAP_DATA, type="tuple[]")
_HEAD = [{"type": "bytes32"}, {"type": "string"}, {"type": "string"},
         {"type": "address"}, {"type": "uint256"}]

#: The GenericSwapFacet / GenericSwapFacetV3 entry points. Selectors are derived
#: from these shapes, never pasted (the test pins the 4-byte values).
_FACETS = {
    "swapTokensGeneric": _SWAP_DATA_LIST,
    "swapTokensSingleV3ERC20ToERC20": _SWAP_DATA,
    "swapTokensSingleV3ERC20ToNative": _SWAP_DATA,
    "swapTokensSingleV3NativeToERC20": _SWAP_DATA,
    "swapTokensMultipleV3ERC20ToERC20": _SWAP_DATA_LIST,
    "swapTokensMultipleV3ERC20ToNative": _SWAP_DATA_LIST,
    "swapTokensMultipleV3NativeToERC20": _SWAP_DATA_LIST,
}


@functools.lru_cache(maxsize=1)
def _by_selector() -> dict:
    # Lazy: keccak needs the [crypto] extra, and every core module must import bare.
    return {abi.selector(abi.signature_of(name, _HEAD + [steps])): (name, steps)
            for name, steps in _FACETS.items()}


@dataclass(frozen=True)
class LifiSwap:
    facet: str
    receiver: str
    min_amount: int
    receiving_asset: str


def decode(data) -> Optional[LifiSwap]:
    """The decoded swap, or ``None`` for a selector this module does not know."""
    text = str(data or "").lower()
    if not text.startswith("0x") or len(text) < 10:
        return None
    known = _by_selector().get(text[:10])
    if known is None:
        return None
    name, steps_field = known
    try:
        _tid, _integrator, _referrer, receiver, min_amount, steps = abi.decode(
            _HEAD + [steps_field], "0x" + text[10:])
    except Exception as exc:
        raise ValueError(f"LI.FI {name} calldata does not decode ({exc})") from None
    last = steps[-1] if steps_field is _SWAP_DATA_LIST else steps
    if steps_field is _SWAP_DATA_LIST and not steps:
        raise ValueError(f"LI.FI {name} calldata carries no swap step")
    return LifiSwap(facet=name, receiver=str(receiver).lower(),
                    min_amount=int(min_amount), receiving_asset=str(last[3]).lower())


def floor_refusal(data, *, receiver: str, token_out: Optional[str],
                  floor: int) -> Optional[str]:
    """Why this LI.FI call may not be signed, or ``None``.

    The caller runs this only for a call to the pinned LI.FI diamond that
    declares an output floor. An unknown selector refuses (085 W8 LEAD-1): its
    on-chain minimum cannot be read, and the simulated floor alone can be gamed.
    """
    try:
        swap = decode(data)
    except ValueError as exc:
        return str(exc)
    if swap is None:
        return unknown_selector_refusal(data)
    if swap.receiver != str(receiver or "").lower():
        return (f"the LI.FI calldata pays its output to {swap.receiver}, not the "
                f"wallet {receiver}")
    if token_out and swap.receiving_asset != str(token_out).lower():
        return (f"the LI.FI calldata buys {swap.receiving_asset}, not the declared "
                f"{token_out}")
    if swap.min_amount <= 0 or swap.min_amount < int(floor):
        return (f"the LI.FI calldata enforces a minimum output of {swap.min_amount}, "
                f"below the floor {floor} — the chain would accept a worse fill than "
                f"was agreed")
    return None


def unknown_selector_refusal(data) -> str:
    """The refusal for a diamond call whose swap layout this module does not know."""
    text = str(data or "").lower()
    sel = text[:10] if text.startswith("0x") and len(text) >= 10 else "(none)"
    return (f"the LI.FI diamond call uses selector {sel}, which is not a known "
            f"same-chain swap facet — its on-chain minimum output cannot be read, "
            f"and a floor judged only on a simulation can be gamed (add the facet "
            f"to core/wallet/lifi_calldata.py _FACETS if LI.FI shipped a new one)")
