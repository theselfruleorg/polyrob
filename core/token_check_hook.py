"""Token-ownership check hook for the identity mapper.

``modules`` may not import ``tools`` (see ``tests/test_layering_ratchet.py``),
so the NFT-gate check the identity mapper (``modules/auth/identity_mapper.py``)
runs is registered from above. It lives in ``core`` (not ``modules/auth``) so the
registrar does not execute ``modules/auth/__init__`` (siwe_auth needs web3,
which the base install lacks). The Alchemy tool module calls
:func:`register_token_checker` at import. With no
registration the mapper behaves as it does when the Alchemy tool is absent
(it logs a warning and leaves the tier unchanged).
"""

from typing import Any, Awaitable, Callable, Dict, Optional

# (tool_instance, wallet_address) -> awaitable result dict with keys
# has_token / token_count / token_ids / contract_address.
TokenChecker = Callable[[Any, str], Awaitable[Dict[str, Any]]]

_checker: Optional[TokenChecker] = None


def register_token_checker(fn: Optional[TokenChecker]) -> None:
    """Install the token-ownership checker (last registration wins)."""
    global _checker
    if fn is not None and not callable(fn):
        raise TypeError("token checker must be callable")
    _checker = fn


def token_checker() -> Optional[TokenChecker]:
    """Return the registered checker, or ``None`` when no tool registered one."""
    return _checker
