"""066 P0.2 — key material leaves ``os.environ`` once the wallet config loads.

Before this, ``AGENT_WALLET_MASTER_SEED`` (and the deposit seed, and the
EIP-8004 signing key) sat in ``os.environ`` for the whole life of the agent
process, so every child that forgot ``build_child_env`` — and the Playwright
Node driver, which copies ``os.environ`` wholesale — inherited it.

The rule now: the FIRST time :func:`core.wallet.config.load_wallet_config`
reads the process env, it calls :func:`take_custody_secrets`, which moves the
four names below out of ``os.environ`` into a module-private holder and sets
the "custody loaded" latch. Every later reader asks :func:`custody_secret`
(the process env first — a value set AFTER the load, e.g. by
``polyrob wallet init``, wins and is taken in turn — then the holder), never
``os.environ`` directly.

What this does NOT do: ``/proc/<pid>/environ`` is the EXEC-TIME block, so a
pop cannot remove the seed from it. That read is closed by
:func:`core.security.process_hardening.harden_custody_process` (non-dumpable).
Both are needed; neither replaces the other.

Dependency-light on purpose (os + threading only): ``core.secret_scrub`` and
``core.security.host_execution`` import it on hot paths.
"""
import os
import threading
from typing import Dict, Mapping, Optional

#: The names that hold signing material. Popped from ``os.environ`` at load.
#: ``MCP_ENCRYPTION_KEY`` is deliberately NOT here: the web and email units
#: decrypt with it too, and it moves with the exchange keys (066 Phase 3, D3).
CUSTODY_SECRET_ENV = (
    "AGENT_WALLET_MASTER_SEED",
    "PAYMENT_MASTER_SEED",
    "MASTER_SEED",
    "EIP8004_AGENT_PRIVATE_KEY",
)

_lock = threading.Lock()
_held: Dict[str, str] = {}
_loaded = False


def take_custody_secrets() -> None:
    """Move every custody secret out of ``os.environ`` and set the latch.

    Idempotent. A name that is set again in ``os.environ`` after an earlier
    take (``wallet init`` writes the new seed there) replaces the held value —
    the same "process env wins" order :func:`custody_secret` reads with.
    """
    global _loaded
    with _lock:
        for name in CUSTODY_SECRET_ENV:
            value = os.environ.pop(name, None)
            if value:
                _held[name] = value
        _loaded = True


def discard_custody_secrets() -> list:
    """066 P2 ``WALLET_SIGNER=remote``: this process must hold NO key material.

    Removes every custody secret from ``os.environ`` AND from the holder, sets
    the latch, and returns the names that were present (for a loud log). The
    exec-time ``/proc/<pid>/environ`` block cannot be edited — the cut-over
    removes ``wallet.env`` from the agent unit so it is not there either.
    """
    global _loaded
    with _lock:
        present = [n for n in CUSTODY_SECRET_ENV if os.environ.get(n) or _held.get(n)]
        for name in CUSTODY_SECRET_ENV:
            os.environ.pop(name, None)
        _held.clear()
        _loaded = True
    return present


def holds_custody_secret() -> bool:
    """True when this process still holds any custody secret (env or holder)."""
    with _lock:
        return any(os.environ.get(n) or _held.get(n) for n in CUSTODY_SECRET_ENV)


def repop_after_env_load() -> None:
    """Re-apply the pop after a later ``load_env`` layered a dotenv file.

    Local mode loads ``~/.polyrob/.env`` (which holds the seed after
    ``wallet init``) with ``override=False``; a second ``load_env`` in the same
    process would put the popped value straight back. A no-op until the latch
    is set, so a process that never loaded the wallet keeps today's behaviour.

    066 P2: under ``WALLET_SIGNER=remote`` a re-appearing secret is DISCARDED,
    never held — the agent process holds no key in that mode.
    """
    if _loaded:
        from core.signer import MODE_REMOTE, signer_mode
        if signer_mode() == MODE_REMOTE:
            discard_custody_secrets()
        else:
            take_custody_secrets()


def custody_loaded() -> bool:
    """True once this process loaded the wallet config and took the secrets."""
    return _loaded


def custody_secret(name: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The value of a custody secret for an in-process reader.

    ``env`` given and not the process env → read ONLY that mapping (a test,
    ``wallet_continuity.py``'s merged env-file dict): the holder belongs to
    this process's own env, never to someone else's mapping.
    """
    if env is not None and env is not os.environ:
        return env.get(name) or None
    value = os.environ.get(name)
    if value:
        return value
    with _lock:
        return _held.get(name) or None


def custody_environ() -> Dict[str, str]:
    """``dict(os.environ)`` with the held secrets filled back in.

    For the in-process diagnostics that take a whole env mapping
    (``doctor_report``, ``flags_report``) and would otherwise report a loaded
    wallet as "seed missing". NEVER hand this to a child process.
    """
    out = dict(os.environ)
    with _lock:
        for name, value in _held.items():
            out.setdefault(name, value)
    return out


def secrets_in_process_env() -> list:
    """The custody secret names still present in ``os.environ`` (for status)."""
    return [n for n in CUSTODY_SECRET_ENV if os.environ.get(n)]


def _reset_for_tests() -> None:
    global _loaded
    with _lock:
        _held.clear()
        _loaded = False
