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
