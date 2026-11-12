"""Which ERC-8004 identities this instance speaks for — one identity in two entries (proposal
115 W6, finding B2).

Two sources, both backed by the chain, neither a configuration claim:

* the ACCOUNT identities: an ERC-6551P NFT the treasury holds owns an identity in its account
  (the collection registered it at mint). The holdings watch (``core.wallet.nft_holdings``)
  records the agentId and whether the account still owns it (``agent_id``, ``identity_held``
  on each tracked row; read on chain each pass, at most one watch interval old);
* the instance's OWN identity: the record ``register_agent`` writes from a confirmed receipt
  (``core.instance.load_erc8004_record``).

Readers: the instance's registration file names each account identity (a ``registrations``
entry, so the two entries link), ``register_agent`` names them in its quote, and the commerce
completion message names where to leave feedback on chain (:func:`feedback_line`).

⚠️ An unreadable holdings file RAISES in :func:`account_identities` (never "holds none");
:func:`feedback_target` turns any failure into None, so the buyer gets no line rather than a
guessed one. Feedback needs no authorization from the agent: the Reputation Registry takes
``giveFeedback`` from any address except the identity's owner and its operators.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def account_identities(home_dir=None, user_id=None, instance_id=None) -> List[Dict[str, Any]]:
    """The identities the accounts of this treasury's tracked NFTs own, oldest NFT first:
    ``{chain, chain_id, collection, token_id, account, agent_id, registry}``. Empty while
    ``AGENT_NFT_ENABLED`` is off (the watch does not run, so its rows may be stale). Raises when
    the holdings file is unreadable."""
    from core.wallet import erc8004, nft_holdings
    if not nft_holdings.enabled():
        return []
    out = []
    for row in nft_holdings.tracked(home_dir, user_id, instance_id):
        agent_id = row.get("agent_id")
        if row.get("identity_held") is not True or isinstance(agent_id, bool) \
                or not isinstance(agent_id, int):
            continue
        try:
            registry = erc8004.resolve_identity_registry(row.get("chain"))
        except ValueError:
            continue    # no pinned registry on that chain: nothing to name
        out.append({"chain": row["chain"], "chain_id": int(row["chain_id"]),
                    "collection": row.get("collection"), "token_id": row.get("token_id"),
                    "account": row["account"], "agent_id": int(agent_id),
                    "registry": registry})
    return out


def own_identity(home_dir=None, instance_id=None) -> Optional[Dict[str, Any]]:
    """The instance wallet's own identity from its receipt record, or None (no record, or one
    that names no pinned chain)."""
    from core.instance import load_erc8004_record, resolve_instance_id
    from core.runtime_paths import resolve_data_home
    from core.wallet import erc8004
    rec = load_erc8004_record(home_dir if home_dir is not None else resolve_data_home(),
                              instance_id or resolve_instance_id())
    if not rec or rec.get("agent_id") is None or not rec.get("chain"):
        return None
    row = erc8004.registry_for(rec["chain"])
    if row is None or str(rec.get("registry") or "").lower() != row.identity.lower():
        return None
    return {"chain": row.chain, "chain_id": row.chain_id, "account": None,
            "agent_id": int(rec["agent_id"]), "registry": row.identity}


def feedback_target(home_dir=None) -> Optional[Dict[str, Any]]:
    """Where a buyer leaves feedback on chain: the first account identity, else the instance's
    own; with ``reputation`` (the pinned Reputation Registry of that chain). None when there is
    none or it cannot be read. Never raises."""
    from core.wallet import erc8004
    try:
        rows = account_identities(home_dir)
        target = rows[0] if rows else own_identity(home_dir)
        if target is None:
            return None
        return dict(target, reputation=erc8004.resolve_reputation_registry(target["chain"]))
    except Exception:  # noqa: BLE001 — no line is better than a guessed one
        import logging
        logging.getLogger(__name__).debug("held_identity: no feedback target", exc_info=True)
        return None


def feedback_line(target: Optional[Dict[str, Any]]) -> str:
    """One short sentence naming where to leave feedback on chain, or "" without a target."""
    if not target:
        return ""
    return (f"To rate this agent on chain: giveFeedback for agentId {target['agent_id']} on the "
            f"ERC-8004 Reputation Registry {target['reputation']} ({target['chain']}, chain id "
            f"{target['chain_id']}).")


__all__ = ["account_identities", "feedback_line", "feedback_target", "own_identity"]
