"""The resource call's receipt (099 W2) — ``TxIntent.is_resource_call``.

An order run may call a contract RESOURCE of its offer with zero value (mint to
the buyer, register, record). Nothing moves, so — the registration precedent —
the RECEIPT is the shape. The simulation must show:

* no native movement of the signer beyond dust, and nothing of the signer's
  moving at all: no token transfer or approval, no NFT out or in, no operator
  grant, no Permit2 grant (there is deliberately no grant verb, and a resource
  call adds none);
* every event emitted by the resource contract itself (a call that makes some
  OTHER contract act is not a resource call), and none of the control-plane or
  grant events (:data:`FORBIDDEN_TOPICS`);
* every token the resource emits (ERC-20/721 ``Transfer``, ERC-1155
  ``TransferSingle``/``TransferBatch``) goes to the order's recipient — or, for
  an offer with no recipient, no token is emitted at all.

WHICH function may be called is pinned per resource at go-live (the selectors
the owner saw on the live card); the kernel's offer step checks it
(``core.money.offer_gate``).

Tier-0: imports nothing above ``core``.
"""
from __future__ import annotations

from typing import Optional

_NATIVE_DUST_WEI = 10 ** 12
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOPIC_TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TOPIC_TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
#: Control-plane and grant events a resource call may never emit: a fulfillment run
#: never hands the contract (or a claim on its tokens) to anyone.
FORBIDDEN_TOPICS = {
    "0x8be0079c531659141344cd1fd0a4f28419497f9722a3daafe3b4186f6b6457e0": "OwnershipTransferred",
    "0x2f8788117e7eff1d82e926ec794901d17c78024a50270940304540a733656f0d": "RoleGranted",
    "0xbd79b86ffe0ab8e8776151514217cd7cacd52c909f66475c3af44e129f0b00ff": "RoleAdminChanged",
    "0xbc7cd75a20ee27fd9adebab32041f755214dbc6bffa90cc0225b39da2e5c2d3b": "Upgraded",
    "0x7e644d79422f17c01e4894b5f4f588d331ebfa28653d42ae832dc59e38c9798f": "AdminChanged",
    "0x1cf3b03a6cf19fa2baba4df148e9dcabedea7f8a5c07840e207e5c089be95d3e": "BeaconUpgraded",
    "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31": "ApprovalForAll",
    "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925": "Approval",
}


def _addr(topic: str) -> str:
    return "0x" + str(topic).lower()[-40:]


def assert_simulation(intent, deltas, holder: str, recipient: Optional[str]) -> Optional[str]:
    """``None`` when the simulated call is a resource call; else the refusal."""
    native = int(getattr(deltas, "native_delta", 0) or 0)
    if abs(native) > _NATIVE_DUST_WEI:
        return (f"the resource call moved {native} wei of the signer's native balance — a "
                f"resource call sends no value")
    moved = [name for name, rows in (
        ("token transfers", getattr(deltas, "holder_transfers", ())),
        ("token approvals", getattr(deltas, "holder_approvals", ())),
        ("NFT moves out", getattr(deltas, "holder_nft_out", ())),
        ("NFT arrivals", getattr(deltas, "holder_nft_in", ())),
        ("operator approvals", getattr(deltas, "holder_operator_grants", ())),
        ("NFT approvals", getattr(deltas, "holder_nft_approvals", ())),
        ("Permit2 approvals", getattr(deltas, "holder_permit2_grants", ())),
        ("ERC-6909 approvals", getattr(deltas, "holder_6909_approvals", ()))) if rows]
    moved += [f"token {t} {d:+d}" for t, d in
              (getattr(deltas, "token_deltas", None) or {}).items() if d]
    if moved:
        return (f"the resource call touches the signer's assets ({', '.join(moved)}) — it "
                f"moves nothing but gas")
    contract = str(getattr(intent, "to", "") or "").lower()
    want = str(recipient or "").lower()
    for log in getattr(deltas, "logs", None) or ():
        emitter = str(log.get("address") or "").lower()
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        if emitter != contract:
            return (f"the call makes {emitter} emit an event — a resource call touches only "
                    f"the resource {contract}")
        if not topics:
            continue
        if topics[0] in FORBIDDEN_TOPICS:
            return (f"the call emits {FORBIDDEN_TOPICS[topics[0]]} — a resource call never "
                    f"changes who controls the contract or grants a claim on its tokens")
        to = None
        if topics[0] == TOPIC_TRANSFER and len(topics) >= 3:
            to = _addr(topics[2])
        elif topics[0] in (TOPIC_TRANSFER_SINGLE, TOPIC_TRANSFER_BATCH) and len(topics) >= 4:
            to = _addr(topics[3])
        if to is None:
            continue
        if not want:
            return "this offer has no recipient, yet the call emits a token transfer"
        if to != want:
            return (f"the call sends a token to {to}, not to the order's recipient {want}")
    return None


__all__ = ["assert_simulation"]
