"""066 P3 — Hyperliquid ORDER signing through ``polyrob-signer``.

With ``WALLET_SIGNER=remote`` the agent's Hyperliquid key lives in the signer.
``hyperliquid.exchange.Exchange`` signs every L1 action through the module-level
``sign_l1_action``; this wraps that one name so an ``Exchange`` built on a
:class:`core.signer.remote.RemoteAccount` sends the ACTION (not a hash) to the
signer, which re-hashes it itself and signs only order-shaped actions
(``core.signer.server.HL_ORDER_ACTIONS``). A user-signed action — a withdrawal,
a USD transfer, an agent approval — reaches ``RemoteAccount.sign_message`` and
refuses: the signer has no schema for it.

A LocalAccount (``local``/``shadow``) is passed straight to the original.
Idempotent; a no-op when the SDK is not installed.
"""
import functools

_INSTALLED = False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import hyperliquid.exchange as _exchange
    except Exception:
        return False
    original = _exchange.sign_l1_action

    @functools.wraps(original)
    def sign_l1_action(wallet, action, active_pool, nonce, expires_after, is_mainnet):
        remote = getattr(wallet, "sign_hl_l1_action", None)
        if callable(remote):
            return remote(action, active_pool, nonce, expires_after, is_mainnet)
        return original(wallet, action, active_pool, nonce, expires_after, is_mainnet)

    _exchange.sign_l1_action = sign_l1_action
    _INSTALLED = True
    return True
