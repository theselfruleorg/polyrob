"""Signer approvals in the ONE owner queue (066 §5.3, owner decision D5).

A request above the signer's hard cap is held by ``polyrob-signer`` as a
pending approval. It surfaces in the SAME union every owner seat lists
(``tools.controller.approval_queue.all_pending`` → ``polyrob owner pending``,
Telegram ``/pending``), as kind ``signer_approval``, and it is decided by the
SAME decider (``decide_pending``) — there is no second approval mechanism.

What differs is WHO can decide it: the signer accepts ``approvals.decide`` only
from uid 0 on its socket. ``sudo polyrob owner promote signer_approval <id>`` on
the box works; a Telegram ``/approve`` runs inside the agent process (not root)
and is refused by the signer with that reason — which is the point: code in the
agent process can never unlock a spend above the hard cap.

Only consulted when ``WALLET_SIGNER`` is not ``local``.
"""
from typing import Any, Dict, List, Tuple

KIND = "signer_approval"


def pending_items(client=None) -> List[Dict[str, Any]]:
    """The signer's pending approvals, shaped like the union's other items.
    Raises when the signer cannot be read (the union NAMES that source)."""
    if client is None:
        from core.signer.client import SignerClient
        client = SignerClient(timeout=5.0)
    rows = client.call("approvals.list").get("pending") or []
    out = []
    for row in rows:
        preview = f"signer hard cap: ${float(row.get('amount_usd') or 0):.2f} — {row.get('summary', '')}"
        out.append({"kind": KIND, "id": str(row.get("id")), "chars": len(preview),
                    "preview": preview[:300], "tool": "signer.evm_send"})
    return out


def decide(approval_id: str, *, approve: bool, client=None) -> Tuple[bool, str]:
    from core.signer.client import SignerClient, SignerRefused, SignerUnavailable
    if client is None:
        client = SignerClient(timeout=10.0)
    try:
        result = client.call("approvals.decide", {"id": str(approval_id), "grant": bool(approve)})
    except SignerRefused as exc:
        if exc.code == "not_root":
            return False, (f"signer approval {approval_id} must be decided on the box as root: "
                           f"`sudo polyrob owner promote {KIND} {approval_id}` ({exc.reason})")
        return False, f"signer approval {approval_id}: {exc.reason}"
    except SignerUnavailable as exc:
        return False, f"polyrob-signer did not answer: {exc}"
    if approve:
        return True, (f"signer approval {approval_id} GRANTED (${float(result.get('amount_usd') or 0):.2f})"
                      f" — one use; the agent's retry of the same request will be signed")
    return True, f"signer approval {approval_id} denied"
