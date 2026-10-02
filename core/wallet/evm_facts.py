"""EVM contract FACTS straight from the chain (071 W2 §3.6) — no provider, no key.

Who can change a token after you buy it is a storage read, not an opinion:

* **bytecode** — no code at the address means it is not a contract at all;
* **proxy slots** — EIP-1967 implementation / admin / beacon, plus the legacy
  ZeppelinOS slots that Circle's USDC proxies still use (measured 2026-10-02:
  USDC on Base and on Ethereum set ONLY the legacy slots, so an EIP-1967-only
  read would have called USDC "not a proxy");
* **owner()** — the zero address means renounced; a revert means the contract
  has no such function (which proves nothing either way);
* the **admin's owner** — a proxy admin is often a ProxyAdmin contract, and the
  key that can upgrade is ITS owner, one hop further.

A slot that reads zero proves only that THIS slot is unused: a custom proxy can
keep its implementation anywhere. The result says "no standard proxy slot set",
never "not upgradeable".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

ZERO = "0x" + "0" * 40

#: name -> storage slot. Order matters only for rendering.
PROXY_SLOTS: Dict[str, str] = {
    "eip1967_implementation": "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
    "eip1967_admin": "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103",
    "eip1967_beacon": "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50",
    "zos_implementation": "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3",
    "zos_admin": "0x10d6a54a4754c8869d6886b5f5d7fbfa5b4522237ea5c60d11bc4e7a1ff9390b",
}

OWNER_SELECTOR = "0x8da5cb5b"  # owner()


def slot_address(raw) -> Optional[str]:
    """A 32-byte storage word -> the address in its low 20 bytes, ``ZERO`` for
    an empty slot, or None when the word is unreadable."""
    if not isinstance(raw, str) or not raw.startswith("0x"):
        return None
    body = raw[2:]
    if body == "":
        return None
    try:
        int(body, 16)
    except ValueError:
        return None
    body = body.rjust(64, "0")[-40:]
    return "0x" + body.lower()


@dataclass
class EvmFacts:
    address: str
    chain: str
    has_code: Optional[bool] = None
    #: slot name -> address (``ZERO`` when the slot is empty); a slot that
    #: could not be read is ABSENT from the dict.
    slots: Dict[str, str] = field(default_factory=dict)
    #: "renounced" | "set" | "no_owner_function" | None (unread)
    owner_state: Optional[str] = None
    owner: Optional[str] = None
    admin_owner: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    @property
    def implementation(self) -> Optional[str]:
        for k in ("eip1967_implementation", "zos_implementation"):
            v = self.slots.get(k)
            if v and v != ZERO:
                return v
        return None

    @property
    def admin(self) -> Optional[str]:
        for k in ("eip1967_admin", "zos_admin"):
            v = self.slots.get(k)
            if v and v != ZERO:
                return v
        return None

    @property
    def beacon(self) -> Optional[str]:
        v = self.slots.get("eip1967_beacon")
        return v if v and v != ZERO else None

    @property
    def proxy_kind(self) -> Optional[str]:
        """"eip1967" | "eip1967_beacon" | "zos_legacy" | "none_found" | None.

        None = not every slot could be read, so "none found" cannot be said.
        """
        if self.slots.get("eip1967_implementation", ZERO) != ZERO:
            return "eip1967"
        if self.beacon:
            return "eip1967_beacon"
        if self.slots.get("zos_implementation", ZERO) != ZERO:
            return "zos_legacy"
        if len(self.slots) < len(PROXY_SLOTS):
            return None
        return "none_found"


def _rpc_for(chain: str) -> Callable:
    from core.wallet.onchain import _rpc as call, rpc_url_for_chain
    url = rpc_url_for_chain(chain)
    if not url:
        raise RuntimeError(f"no RPC for {chain}")

    def rpc(method: str, params: list, timeout: float = 6.0):
        return call(url, method, params, timeout)
    return rpc


def _rpc(chain: str):
    """Seam blocked in unit tests."""
    return _rpc_for(chain)


def _owner_of(rpc: Callable, address: str):
    """(state, owner). A revert / empty return = the function is absent."""
    try:
        raw = rpc("eth_call", [{"to": address, "data": OWNER_SELECTOR}, "latest"])
    except Exception as exc:
        text = str(exc).lower()
        if "revert" in text or "execution" in text:
            return "no_owner_function", None
        raise
    if not isinstance(raw, str) or raw in ("0x", ""):
        return "no_owner_function", None
    addr = slot_address(raw)
    if addr is None:
        return None, None
    return ("renounced" if addr == ZERO else "set"), (None if addr == ZERO else addr)


def read_contract(chain: str, address: str, *, rpc: Optional[Callable] = None) -> EvmFacts:
    """Every read is independent; one failed read is recorded in ``errors`` and
    leaves its field unknown. Raises only when NO read could be made at all."""
    call = rpc or _rpc(chain)
    facts = EvmFacts(address=address, chain=chain)
    try:
        code = call("eth_getCode", [address, "latest"])
        facts.has_code = isinstance(code, str) and code not in ("0x", "0x0", "")
    except Exception as exc:
        facts.errors.append(f"eth_getCode: {exc.__class__.__name__}")
    if facts.has_code is False:
        return facts
    for name, slot in PROXY_SLOTS.items():
        try:
            got = slot_address(call("eth_getStorageAt", [address, slot, "latest"]))
        except Exception as exc:
            facts.errors.append(f"{name}: {exc.__class__.__name__}")
            continue
        if got is not None:
            facts.slots[name] = got
    try:
        facts.owner_state, facts.owner = _owner_of(call, address)
    except Exception as exc:
        facts.errors.append(f"owner(): {exc.__class__.__name__}")
    admin = facts.admin
    if admin:
        try:
            code = call("eth_getCode", [admin, "latest"])
            if isinstance(code, str) and code not in ("0x", "0x0", ""):
                state, who = _owner_of(call, admin)
                if state == "set":
                    facts.admin_owner = who
                elif state == "renounced":
                    facts.admin_owner = ZERO
        except Exception as exc:
            facts.errors.append(f"admin owner(): {exc.__class__.__name__}")
    if facts.has_code is None and not facts.slots and facts.owner_state is None:
        raise RuntimeError("; ".join(facts.errors) or "no read succeeded")
    return facts
