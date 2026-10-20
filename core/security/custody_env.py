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

Dependency-light on purpose (os + threading only at import): ``core.secret_scrub``
and ``core.security.host_execution`` import it on hot paths. The 085 W1 file
record is fed by ``load_env`` and ``core.env_file.upsert_env_var``
(:func:`note_env_file`); only :func:`readable_custody_file` reads those files.
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
#: 085 W1: every env file this process layered (``load_env``) or wrote a custody
#: secret into (``core.env_file.upsert_env_var``: ``polyrob wallet init``,
#: ``polyrob config set``, the console). PATHS only, never a value: each check
#: reads the file again, so a seed written after the load counts and a seed
#: removed from the file stops counting.
_env_files: list = []
_loaded = False

SOURCE_PROCESS_ENV = "process_env"
SOURCE_DOTENV_PREFIX = "dotenv:"


def note_env_file(path, values: Optional[Mapping[str, Optional[str]]] = None) -> None:
    """Record *path* as an env file this process layered or wrote (085 W1).

    ``polyrob.app.bootstrap.load_env`` calls this for every ladder file it
    layers, and ``core.env_file.upsert_env_var`` for every custody secret it
    writes. *values* is accepted for the callers' convenience and not kept:
    the gap re-reads the file, so no copy of the seed stays here."""
    key = str(path)
    with _lock:
        if key not in _env_files:
            _env_files.append(key)


def _file_custody_values(path: str) -> Dict[str, str]:
    """``{name: value}`` for each custody secret *path* holds now ({} when the
    file is gone or this UID cannot read it). Never logged, never kept."""
    try:
        if not os.access(path, os.R_OK):
            return {}
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except (OSError, ValueError):
        return {}
    try:
        import io
        from dotenv import dotenv_values  # parse parity with load_env
        parsed = dotenv_values(stream=io.StringIO(text))
    except Exception:
        parsed = {}
        for line in text.splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep:
                parsed[name.strip()] = value.strip().strip("'\"")
    return {n: str(parsed[n]) for n in CUSTODY_SECRET_ENV if parsed.get(n)}


def _recorded_files() -> list:
    with _lock:
        return list(_env_files)


def _dotenv_source(name: str, value: str) -> str:
    """``dotenv:<path>`` when a recorded env file holds *name* = *value* now,
    else ``process_env`` (a shell export or the systemd ``EnvironmentFile``)."""
    for path in _recorded_files():
        if _file_custody_values(path).get(name) == value:
            return SOURCE_DOTENV_PREFIX + path
    return SOURCE_PROCESS_ENV


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


def custody_secret_sources() -> Dict[str, str]:
    """``{name: source}`` for every custody secret this process holds (085 W1):
    ``process_env`` or ``dotenv:<path>``, never a value. A secret still in
    ``os.environ`` (the wallet config has not loaded yet) counts too."""
    with _lock:
        held = dict(_held)
    out = {}
    for name in CUSTODY_SECRET_ENV:
        value = os.environ.get(name) or held.get(name)
        if value:
            out[name] = _dotenv_source(name, value)
    return out


def readable_custody_file() -> Optional[str]:
    """The path of a recorded env file that holds a custody secret NOW and that
    the current UID can read, or None (085 W1).

    A same-UID host child can open that file, whatever the in-memory isolation
    does — and whatever this process holds: a seed left in the file under the
    remote signer, or an older seed than the one systemd set, is still a key a
    child can read. A seed only in the process env (systemd ``EnvironmentFile``,
    root-owned) or no seed in any recorded file: None.

    An old ``polyrob update`` snapshot copy is checked by the caller
    (``core.security.host_execution``) with :func:`file_holds_custody_secret`:
    this module never imports the path tier (the layering cycle)."""
    for path in _recorded_files():
        if _file_custody_values(path):
            return path
    return None


def file_holds_custody_secret(path) -> bool:
    """True when *path* is readable by this UID and holds a custody secret now."""
    return bool(_file_custody_values(str(path)))


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
        _env_files.clear()
        _loaded = False
