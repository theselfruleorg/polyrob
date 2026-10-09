"""The on-chain floor inside a LI.FI diamond swap, read from the SIGNED bytes.

The swap rail asserts its output floor against a simulated receipt
(``TxIntent.min_inflow_raw``). A simulation can be told apart from the real
block, and an aggregator API can encode a minimum of 0 while its JSON reports a
healthy ``toAmountMin``. So for the LI.FI facets whose layout is known, the
minimum the CHAIN will enforce is decoded from the calldata and held to our
floor, and the receiver is held to our own wallet.

Every known facet takes ``(bytes32 transactionId, string integrator,
string referrer, address receiver, uint256 minAmount, SwapData…)``.

An unknown selector returns ``None``: the caller falls back to the measured
inflow floor it already asserts. A natural swap is never refused because LI.FI
shipped a new facet. A KNOWN selector whose body does not decode raises
``ValueError`` — that is malformed, not new.
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

    ``None`` also for an unknown facet: the measured-inflow floor still applies.
    """
    try:
        swap = decode(data)
    except ValueError as exc:
        return str(exc)
    if swap is None:
        return None
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
