"""The ERC-8004 registries — PINNED in code, never taken from configuration.

⚠️ An address in a config file is a claim, not a check. `tools/launchpad/pons.py`
learned this the hard way: web search returns the WRONG generation of a protocol
(Pons **V1** for the V2 factory), so it re-verifies its factory by code hash on
every call and refuses on mismatch. The same discipline applies here, and it
matters more: `register_agent` sends a transaction from the treasury wallet to
whatever address this module returns. If that address were operator-supplied,
one env var would turn a registration verb into "call an arbitrary contract".

So `EIP8004_*_REGISTRY` may only **narrow** to a pinned row. It can confirm a
choice; it can never introduce an address.

The ERC-8004 reference registries are per-chain singletons — the same vanity
address on every mainnet, and a second one on every testnet — which is what
makes pinning them both possible and obviously right.

Addresses verified 2026-09-15 against
https://github.com/erc-8004/erc-8004-contracts. The **Validation** Registry spec
is still under development upstream and is deliberately not pinned here.

⚠️ `robinhood` (4663) is ABSENT, on purpose: this project's own chain registry
carries it, but ERC-8004 has no published deployment there. `registry_for`
returns None rather than a guess — a made-up address on a real money chain is
exactly the failure this module exists to prevent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

#: Same address on every mainnet (a deliberate vanity deployment).
_IDENTITY_MAINNET = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
_REPUTATION_MAINNET = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
#: …and a second, DIFFERENT pair on every testnet. ⚠️ Registering on a testnet
#: at a mainnet address would silently call whatever happens to live there.
_IDENTITY_TESTNET = "0x8004A818BFB912233c491871b3d84c89A494BD9e"
_REPUTATION_TESTNET = "0x8004B663056A597Dffe9eCcC1965A193B7388713"


@dataclass(frozen=True)
class RegistryRow:
    """The two ERC-8004 registries on one chain."""
    chain: str
    chain_id: int
    identity: str
    reputation: str


def _mainnet(chain: str, chain_id: int) -> RegistryRow:
    return RegistryRow(chain, chain_id, _IDENTITY_MAINNET, _REPUTATION_MAINNET)


def _testnet(chain: str, chain_id: int) -> RegistryRow:
    return RegistryRow(chain, chain_id, _IDENTITY_TESTNET, _REPUTATION_TESTNET)


#: chain name (this project's vocabulary, `core/wallet/chains.py`) -> registries.
_ROWS: Dict[str, RegistryRow] = {
    "ethereum": _mainnet("ethereum", 1),
    "base": _mainnet("base", 8453),
    "optimism": _mainnet("optimism", 10),
    "arbitrum": _mainnet("arbitrum", 42161),
    "polygon": _mainnet("polygon", 137),
    # Testnets — where a first registration should always be exercised.
    "ethereum-sepolia": _testnet("ethereum-sepolia", 11155111),
    "base-sepolia": _testnet("base-sepolia", 84532),
    "optimism-sepolia": _testnet("optimism-sepolia", 11155420),
    "arbitrum-sepolia": _testnet("arbitrum-sepolia", 421614),
    "polygon-amoy": _testnet("polygon-amoy", 80002),
}


def supported_chains() -> Tuple[str, ...]:
    """Chains with a pinned ERC-8004 deployment."""
    return tuple(_ROWS)


def registry_for(chain: Optional[str]) -> Optional[RegistryRow]:
    """The pinned row for *chain*, or ``None`` when there is no deployment.

    ``None`` is an honest answer, not a failure: a money chain without an
    ERC-8004 deployment simply cannot carry the identity, and saying so beats
    inventing an address.
    """
    if not chain:
        return None
    return _ROWS.get(str(chain).strip().lower())


def _resolve(chain: Optional[str], env_name: str, field: str) -> str:
    row = registry_for(chain)
    if row is None:
        raise ValueError(
            f"chain {chain!r} has no pinned ERC-8004 registry. Supported: "
            f"{', '.join(supported_chains())}")
    pinned = getattr(row, field)
    override = (os.getenv(env_name) or "").strip()
    if override and override.lower() != pinned.lower():
        # ⚠️ Narrow-only. This refusal is what stops one env var from aiming a
        # treasury-signed transaction at an arbitrary contract.
        raise ValueError(
            f"{env_name}={override} does not match the pinned ERC-8004 "
            f"{field} registry for {chain} ({pinned}). This value may only "
            f"CONFIRM the pinned address, never replace it — a registration is "
            f"a transaction from the treasury wallet, so an arbitrary "
            f"destination here is an arbitrary contract call.")
    return pinned


def resolve_identity_registry(chain: Optional[str]) -> str:
    """The Identity Registry to register against. Raises on an unknown chain or
    a non-matching env override."""
    return _resolve(chain, "EIP8004_IDENTITY_REGISTRY", "identity")


def resolve_reputation_registry(chain: Optional[str]) -> str:
    return _resolve(chain, "EIP8004_REPUTATION_REGISTRY", "reputation")
