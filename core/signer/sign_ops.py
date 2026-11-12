"""``sign.classified`` — the signer's one off-chain signing op (proposal 090).

The agent classifies a request (``core.wallet.sign_policy``) before it asks;
this op classifies the WIRE payload again with the signer's own key address and
signs only class A. The agent's label is not on the wire at all: the class the
signer computes is the class that counts. A, B, C and D are the agent's words for
why it asked; anything but A refuses here (B and C need gates that live in later
phases of 090).

The signer cannot prove the agent fetched the text, so it never believes the
wire's ``fetch_bound``: a challenge that does not name its site is class C here
(the agent signs it in process on a genuine owner turn under ``local``/``shadow``;
under ``remote`` it refuses; shadow logs it as ``agent_only``, which does not count
against the cut-over). Its own bound, whatever the agent says: ``SIGN_DAILY_CAP``
signatures per 24 h. The same text may be signed again: EIP-191 (RFC 6979) and
ed25519 signatures are deterministic, so a repeat reveals nothing new — and a site
with a fixed login text must be able to log in twice.

Not a money op: a class-A text moves no value, so the signer's spend pause does
not stop it (090 D3). Every request — signed, refused or ``verdict_only`` (the
shadow comparison) — is one ``decisions`` row: scheme, class, bound origin and
the digest (``ref='signed'`` marks a real signature). The signature itself is
never logged.
"""
import threading
from typing import Any, Dict

from core.signer import protocol

_KEYS = ("scheme", "payload", "bound_origin", "fetch_bound")
#: Signatures the signer makes per 24 h, across every site.
SIGN_DAILY_CAP = 50
_lock = threading.Lock()


def op_sign_classified(service, body: Dict[str, Any], peer_uid: int) -> Dict[str, Any]:
    from core.signer.server import _Refuse
    from core.wallet.sign_policy import SignContext, SignRequest, classify, _render_signature
    protocol.check_keys(body, required=_KEYS, optional=("verdict_only",))
    if not isinstance(body["payload"], str) or not isinstance(body["bound_origin"], str) \
            or not isinstance(body["fetch_bound"], bool):
        raise protocol.ProtocolError("payload and bound_origin are strings, fetch_bound a bool")
    req = SignRequest.from_wire(body)
    if req.scheme == "solana_message":
        signer = service.wallet.solana_signer(0)
    else:
        signer = service.wallet.operational_signer()
    verdict = classify(req, SignContext(holder=signer.address, now=service._now(),
                                        trust_fetch_bound=False))
    reason = f"{req.scheme} class {verdict.cls} {verdict.decoded.get('domain') or '-'}: " \
             f"{verdict.reason}"
    if not verdict.signable:
        service.store.log_decision(op="sign.classified", digest=verdict.digest or None,
                                   allowed=False, code=protocol.GUARD_REFUSED,
                                   reason=reason, peer_uid=peer_uid)
        raise _Refuse(protocol.GUARD_REFUSED, f"class {verdict.cls}: {verdict.reason}",
                      cls=verdict.cls)
    if body.get("verdict_only"):
        service.store.log_decision(op="sign.classified", digest=verdict.digest, allowed=True,
                                   reason="verdict only: " + reason, peer_uid=peer_uid)
        return {"cls": verdict.cls, "digest": verdict.digest, "address": signer.address}
    with _lock:
        used = service.store.signed_since("sign.classified", service._now() - 86400)
        if used >= SIGN_DAILY_CAP:
            raise _Refuse(protocol.GUARD_REFUSED,
                          f"the signer already signed {used} texts in 24 h (cap {SIGN_DAILY_CAP})")
        raw = signer.sign_message(req.payload.encode("utf-8"))
        service.store.log_decision(op="sign.classified", digest=verdict.digest, allowed=True,
                                   reason=reason, peer_uid=peer_uid, ref="signed")
    return {"cls": verdict.cls, "digest": verdict.digest, "address": signer.address,
            "signature": _render_signature(req.scheme, raw)}


#: Market orders the signer signs per 24 h (111 §8.3: one owner card per order).
SIGN_ORDER_DAILY_CAP = 20


def op_sign_order(service, body: Dict[str, Any], peer_uid: int) -> Dict[str, Any]:
    """``sign.order`` — a class-O pinned market ``Listing`` (proposal 111 §8.3, D8).

    The signer reads every fact itself (``core.wallet.sale_orders.read_listing_facts``: its own
    RPC for the chain, the root-owned collection registry, the treasury it signs for) and runs
    the SAME classifier as the agent (``sign_policy.classify`` with those facts). Anything but
    class O refuses; nothing the wire carries is a fact. A money op: under the signing lock and
    the signer's spend pause (an order is an obligation), bounded by
    :data:`SIGN_ORDER_DAILY_CAP` per 24 h. ``verdict_only`` answers the class without signing
    (the shadow comparison) and never opens an approval.

    An order sells an NFT no cap can price, so — like an NFT act in ``evm.send`` — every
    signature needs the owner's INDEPENDENT signer approval for this exact listing digest (the
    market's own): with none live, one is opened and the request refuses with its id; a granted
    one is consumed (one use) before the signature leaves."""
    from core.signer.server import _Refuse
    from core.wallet import collection_registry
    from core.wallet.sign_policy import O, SignContext, SignRequest, classify
    import core.wallet.sale_orders as orders
    protocol.check_keys(body, required=("chain", "payload"), optional=("verdict_only",))
    chain = body["chain"]
    if not isinstance(chain, str) or not isinstance(body["payload"], dict):
        raise protocol.ProtocolError("chain is a string and payload an object")
    service._chain_row(chain)
    signer = service.wallet.operational_signer()
    rpc_fn = getattr(service, "order_rpc_fn", None)
    if rpc_fn is None:
        from core.wallet.simulation import _default_rpc_for
        rpc_fn = _default_rpc_for
    try:
        facts = orders.read_listing_facts(rpc_fn(chain), collection_registry, chain=chain,
                                          payload=body["payload"], holder=signer.address)
    except orders.OrderRefused as exc:
        facts = orders.ListingFacts(0, refusal=str(exc))
    req = SignRequest(scheme="eip712", payload=body["payload"])
    verdict = classify(req, SignContext(holder=signer.address, order_facts=facts))
    reason = f"eip712 class {verdict.cls} {chain}: {verdict.reason}"
    if verdict.cls != O:
        service.store.log_decision(op="sign.order", digest=verdict.digest or None, allowed=False,
                                   code=protocol.GUARD_REFUSED, reason=reason, peer_uid=peer_uid)
        raise _Refuse(protocol.GUARD_REFUSED, f"class {verdict.cls}: {verdict.reason}",
                      cls=verdict.cls)
    if body.get("verdict_only"):
        service.store.log_decision(op="sign.order", digest=verdict.digest, allowed=True,
                                   reason="verdict only: " + reason, peer_uid=peer_uid)
        return {"cls": verdict.cls, "digest": verdict.digest, "address": signer.address}
    grant = _order_grant(service, verdict, chain=chain, holder=signer.address,
                         peer_uid=peer_uid)
    used = service.store.signed_since("sign.order", service._now() - 86400)
    if used >= SIGN_ORDER_DAILY_CAP:
        raise _Refuse(protocol.GUARD_REFUSED,
                      f"the signer already signed {used} market orders in 24 h "
                      f"(cap {SIGN_ORDER_DAILY_CAP})")
    typed = body["payload"]
    types = {k: v for k, v in typed["types"].items() if k != "EIP712Domain"}
    signature = signer.sign_typed_data(typed["domain"], types, typed["message"])
    if not orders.recovers(verdict.digest, signature, signer.address):
        raise _Refuse(protocol.INTERNAL, "the order signature does not recover over the "
                                         "market's digest; not returned")
    # Spend the one-use grant BEFORE the signature leaves or is booked (the x402 rule).
    if not service.store.consume_grant(grant["id"]):
        service.store.log_decision(op="sign.order", digest=verdict.digest, allowed=False,
                                   code=protocol.APPROVAL_REQUIRED, peer_uid=peer_uid,
                                   reason=f"approval {grant['id']} no longer live",
                                   ref=grant["id"])
        raise _Refuse(protocol.APPROVAL_REQUIRED,
                      f"signer approval {grant['id']} is no longer live; nothing was signed",
                      approval_id=grant["id"], digest=verdict.digest)
    service.store.log_decision(op="sign.order", digest=verdict.digest, allowed=True,
                               reason=reason, peer_uid=peer_uid, ref="signed")
    return {"cls": verdict.cls, "digest": verdict.digest, "address": signer.address,
            "signature": signature}


def _order_grant(service, verdict, *, chain: str, holder: str, peer_uid: int) -> Dict[str, Any]:
    """The live GRANTED approval of this exact listing digest (op ``sign.order``), or
    :class:`_Refuse` ``approval_required`` with the id of the approval it opened (or the one
    still pending)."""
    from core.signer.server import _Refuse
    digest = verdict.digest
    existing = service.store.find_approval(digest)
    if existing is not None and existing.get("op") != "sign.order":
        existing = None     # another op's row never unlocks an order
    if existing is not None and existing["state"] == "granted":
        return existing
    d = verdict.decoded
    if existing is None:
        summary = (f"sign.order — a market LISTING signed by {holder}: {d.get('collection')} "
                   f"#{d.get('id')} on {chain}, market {d.get('market')}, price {d.get('price')} "
                   f"raw of {d.get('currency')} (0x0 = ETH), expires {d.get('expiry')}, salt "
                   f"{d.get('salt')}; listing digest {digest}. One use: it signs this listing "
                   f"once.")
        existing = service.store.open_approval(digest=digest, op="sign.order", chain=chain,
                                               amount_usd=0.0, summary=summary,
                                               ttl_sec=service.config.approval_ttl_sec)
    reason = (f"a market listing requires independent signer approval (class O: "
              f"{verdict.reason}; an NFT sale has no price a cap can bound). The owner approves "
              f"it ON THE BOX: `sudo polyrob owner promote signer_approval {existing['id']}`, then "
              f"the same listing is retried. Nothing was signed.")
    service.store.log_decision(op="sign.order", digest=digest, allowed=False,
                               code=protocol.APPROVAL_REQUIRED, reason=reason,
                               peer_uid=peer_uid, ref=existing["id"])
    raise _Refuse(protocol.APPROVAL_REQUIRED, reason, approval_id=existing["id"],
                  digest=digest)
