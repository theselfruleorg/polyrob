"""The Pons "did WE launch this?" probe for ``core.wallet.token_provenance`` (W0).

Core cannot import the tools tier, so the launchpad registers its on-chain
provenance check here. The answer is the factory's own record: a token whose
``getLaunchedToken(token).deployer`` is one of this wallet's addresses is our
launch. Only the DEPLOYER counts — ``creatorFeeRecipient`` is chosen by whoever
launches, so a look-alike could name our wallet there.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_TIMEOUT_S = 6.0


def pons_probe(chain: str, address: str, *, rpc=None) -> Optional[str]:
    """Evidence text when the pinned Pons factory records our wallet as the
    deployer of *address*, else None. Raises nothing it does not have to: the
    caller treats an exception as "no evidence"."""
    from tools.launchpad import pons_abi as P
    if str(chain or "").strip().lower() != P.CHAIN:
        return None
    from core.wallet.token_provenance import own_evm_addresses
    ours = own_evm_addresses()
    if not ours:
        return None  # no known address of ours: nothing to compare, no RPC
    if rpc is None:
        from core.wallet.onchain import _rpc, rpc_url_for_chain

        def rpc(method, params):
            return _rpc(rpc_url_for_chain(P.CHAIN), method, params, timeout=_TIMEOUT_S)
    from tools.launchpad import pons
    record = pons.launched_token(rpc, address)
    if not record:
        return None
    from core.wallet.addresses import same_address
    deployer = str(record.get("deployer") or "")
    if any(same_address(deployer, a) for a in ours):
        return f"Pons factory {P.FACTORY} records deployer {deployer}"
    return None


def register() -> None:
    from core.wallet.token_provenance import register_launch_probe
    register_launch_probe("pons", pons_probe)


register()
