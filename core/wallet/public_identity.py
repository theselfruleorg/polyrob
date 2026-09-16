"""The wallet's PUBLIC half, readable by a process that holds no seed.

Prod splits ``AGENT_WALLET_MASTER_SEED`` out of ``/etc/polyrob/polyrob.env``
into ``/etc/polyrob/wallet.env``, which ONLY ``polyrob.service`` (the agent)
loads. That is the right shape — the console (``polyrob-webview.service``) and
the email surface (``polyrob-email.service``) have no business holding signing
authority — but taken alone it breaks every read that goes THROUGH an address:
``AgentWallet.__init__`` refuses to build without a seed, so
``get_agent_wallet()`` yields nothing and ``defi_data.portfolio``,
``reconcile``, ``/api/webgate/positions``, the status snapshot and
``resolve_treasury_address`` each degrade to "no address to report holdings
for" on a wallet that is funded and working.

An address is PUBLIC. The seeded process writes it here; a seedless process
reads it and gets a wallet that knows WHO it is and can prove nothing.

Three properties the rest of the system depends on:

* **A read never CREATES the file.** Absent means the seeded agent has not run
  yet — a real answer, and the one that keeps a genuinely unconfigured deploy
  failing exactly as loudly as it did before this module existed.
* **The record is checked against a freshly derived address before it counts as
  current.** A stale record after a seed change would be a money-visible lie:
  an owner funding, or an invoice billing to, an address the agent cannot spend
  from. So the writer re-derives rather than trusting what it wrote last time.
* **It is PUBLIC data and it is written 0644.** There is nothing in it to
  protect, and a file the console's unit user cannot read is a file that does
  not do its job.

The path is ``<data_home>/wallet/public_identity.json`` — resolved through the
SAME ``_wallet_data_dir`` the derivation meta and the audit ledger use, so init
time and run time can never disagree about which wallet home is in play.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

FILENAME = "public_identity.json"


def identity_path(data_home: Optional[str] = None) -> str:
    """``<data_home>/wallet/public_identity.json``.

    Resolved at CALL time, never bound at import — an import-time binding
    freezes the home before ``-P``/``POLYROB_DATA_DIR`` can move it
    (``tests/test_home_binding_ratchet.py``).
    """
    from core.wallet.audit_sink import _wallet_data_dir
    return os.path.join(_wallet_data_dir(data_home), FILENAME)


def build_record(wallet: Any) -> Dict[str, Any]:
    """Everything public about *wallet*, derived fresh.

    A venue whose key cannot be derived is OMITTED rather than recorded as
    empty: a reader must be able to tell "this venue has no recorded address"
    from "this venue's address is blank". A record with no address at all is
    refused outright — writing one would replace a good record with a useless
    one and make a working console look unconfigured.
    """
    from core.wallet.agent_wallet import VENUES

    evm: Dict[str, str] = {}
    for venue in sorted(VENUES):
        try:
            evm[venue] = str(wallet.signer_for(venue).address)
        except Exception as exc:
            logger.warning("public identity: no %s address (%s: %s)",
                           venue, type(exc).__name__, exc)

    solana: Optional[str] = None
    try:
        solana = str(wallet.solana_address)
    except Exception as exc:
        # `solders` absent is the ordinary case here, not an incident.
        logger.debug("public identity: no Solana address (%s: %s)",
                     type(exc).__name__, exc)

    if not evm:
        raise ValueError(
            "no EVM address could be derived — refusing to write a public "
            "identity record with nothing in it")

    return {
        "evm": evm,
        "solana": solana,
        "scheme": str(getattr(wallet, "scheme", "") or ""),
        "operational_venue": str(wallet.operational_venue),
        "network": str(getattr(wallet, "network", "") or ""),
        "written_at": time.time(),
    }


def write_public_identity(wallet: Any, data_home: Optional[str] = None) -> Dict[str, Any]:
    """Derive and persist the record atomically. Returns what was written.

    Raises on any failure — the fail-open decision belongs to the caller
    (:func:`maybe_publish`), not to the writer, so a test or a CLI can still
    see why a write did not happen.
    """
    record = build_record(wallet)
    path = identity_path(data_home)
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
        # PUBLIC on purpose: another unit's user has to read it. os.replace
        # preserves the temp file's mode, so set it before the swap.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise
    return record


def read_public_identity(data_home: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The recorded public identity, or None when there is none to have.

    None covers "never written", "unreadable" and "written but empty" — all
    three mean the same thing to a caller: this process cannot know the
    wallet's addresses, and it must say so rather than invent one.
    """
    path = identity_path(data_home)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        logger.warning("public identity: %s is unreadable — treating the "
                       "wallet's addresses as unknown", path, exc_info=True)
        return None
    if not isinstance(raw, dict):
        return None
    evm = raw.get("evm")
    if not isinstance(evm, dict) or not any(evm.values()):
        logger.warning("public identity: %s holds no address — a record with "
                       "nothing in it is not a record", path)
        return None
    return raw


def is_current(record: Optional[Dict[str, Any]], wallet: Any) -> bool:
    """Does *record* still describe *wallet*?

    Deliberately cheap: it re-derives ONE key — the operational venue, which is
    the key every address read wants anyway and which the wallet caches — plus
    the Solana account (a 2048-round PBKDF2, negligible). That is enough to
    catch the case that matters, a changed seed or a changed derivation scheme,
    without paying for all four venues on every process start.
    """
    if not record:
        return False
    try:
        if str(record.get("scheme") or "") != str(getattr(wallet, "scheme", "") or ""):
            return False
        from core.wallet.agent_wallet import VENUES
        evm = record.get("evm") or {}
        if set(evm) != set(VENUES):
            return False
        venue = wallet.operational_venue
        if str(record.get("operational_venue") or "") != str(venue):
            return False
        if str(evm.get(venue) or "") != str(wallet.signer_for(venue).address):
            return False
    except Exception:
        return False
    try:
        solana = str(wallet.solana_address)
    except Exception:
        solana = None
    # A record written before `solders` was installed carries solana=None; once
    # the address becomes derivable it must be recorded, so this is not
    # "equal or absent", it is equal.
    return str(record.get("solana") or "") == str(solana or "")


def maybe_publish(wallet: Any, data_home: Optional[str] = None) -> bool:
    """Write the record unless a current one already exists. Fail-open.

    Called by the factory the moment a SEEDED wallet is built, so the seedless
    units always have something to read and it is never older than the last
    time the agent started. Returns whether a write happened; a failure is a
    logged debug line and never blocks the wallet — a console that cannot show
    an address is a degraded view, but an agent that cannot build its wallet
    because a JSON file would not write is a stopped agent.
    """
    try:
        if is_current(read_public_identity(data_home), wallet):
            return False
        write_public_identity(wallet, data_home)
        return True
    except Exception:
        logger.debug("public identity: publish failed (fail-open)", exc_info=True)
        return False
