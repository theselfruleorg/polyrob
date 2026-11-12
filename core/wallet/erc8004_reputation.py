"""The ERC-8004 reputation of the identity a held NFT's account registered — a READ (115 W5).

Three calls at the view's one block, each on a pinned contract:

* the collection's ``hasIdentity(id)`` / ``agentIdOf(id)`` name the agentId (a collection view,
  authoritative; no log scan);
* the Reputation Registry's ``getClients(agentId)`` lists every address that gave feedback;
* ``getSummary(agentId, clients, "", "")`` gives the count and the summed value. The registry
  REVERTS on an empty client list, so it is not called when nobody gave feedback.

⚠️ A count is not a score. The registry lets any address but the identity's own owner give
feedback, and the HOLDER of the NFT can rate its own agent from its own address. So the line
always shows who the clients are; a reader chooses the clients it trusts.

An unreadable value is UNREADABLE, never "none" (the caller's ``_try`` records why).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from core.wallet import abi, erc8004
# The collection's ``hasIdentity`` / ``agentIdOf`` read lives with the guard's identity rule
# (``core.wallet.identity_card``), so the guard does not pull this read module into its cycle.
from core.wallet.identity_card import agent_id_of  # noqa: F401 — re-exported

_U256 = {"type": "uint256"}


@dataclass(frozen=True)
class Reputation:
    agent_id: int
    clients: Tuple[str, ...]
    count: int = 0
    value: int = 0
    decimals: int = 0

    def line(self) -> str:
        if not self.clients:
            return f"agentId {self.agent_id}: no feedback on chain"
        shown = ", ".join(self.clients[:5]) + (f" and {len(self.clients) - 5} more"
                                               if len(self.clients) > 5 else "")
        value = self.value / (10 ** self.decimals) if self.decimals else self.value
        return (f"agentId {self.agent_id}: {self.count} feedback entr{'y' if self.count == 1 else 'ies'}, "
                f"summed value {value}, from {len(self.clients)} client(s): {shown} — any address "
                f"may give feedback (the holder too): a count is not a score")


def _call(rpc, to: str, name: str, inputs, args, outputs):
    raw = rpc("eth_call", [{"to": to, "data": abi.encode_call(name, inputs, args)}, "latest"])
    if raw in (None, "", "0x"):
        raise ValueError(f"{name}() on {to} returned no data")
    return abi.decode(outputs, raw)


def _evm_key(address) -> Optional[str]:
    text = str(address or "").strip().lower()
    if len(text) == 42 and text.startswith("0x") and all(c in "0123456789abcdef" for c in text[2:]):
        return text
    return None


def split_paying(clients, paid_addresses) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(paying, others)``: the feedback *clients* whose address is in *paid_addresses*
    (the payers of this instance's completed invoices,
    ``modules.x402.paid_payers.paid_payer_addresses``), and the rest. Both keep the
    registry's order and the client's own spelling.

    Matches on the lowercase EVM address only: the registry's clients are EVM addresses, so
    a non-EVM entry never matches. ⚠️ An unreadable store has no paid set: pass ``None``
    and this raises ``TypeError`` — it never reads as "none paid"."""
    if paid_addresses is None:
        raise TypeError("paid_addresses is None: the order store was not read")
    paid = {k for k in (_evm_key(a) for a in paid_addresses) if k}
    paying = tuple(c for c in clients if _evm_key(c) in paid)
    others = tuple(c for c in clients if _evm_key(c) not in paid)
    return paying, others


def read(rpc, chain: str, agent_id: int) -> Reputation:
    registry = erc8004.resolve_reputation_registry(chain)
    (clients,) = _call(rpc, registry, "getClients", [_U256], [int(agent_id)], [{"type": "address[]"}])
    clients = tuple(str(c) for c in clients)
    if not clients:
        return Reputation(int(agent_id), ())
    count, value, decimals = _call(
        rpc, registry, "getSummary",
        [_U256, {"type": "address[]"}, {"type": "string"}, {"type": "string"}],
        [int(agent_id), list(clients), "", ""],
        [{"type": "uint64"}, {"type": "int128"}, {"type": "uint8"}])
    return Reputation(int(agent_id), clients, int(count), int(value), int(decimals))
